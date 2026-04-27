"""Tests for framework.llm_schema — Pydantic models for LLM extraction.

Validates schema instantiation, serialization/deserialization, confidence
enums, outcome/case_type validation, and JSON schema generation.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from framework.llm_schema import (
    EXTRACTION_SYSTEM_PROMPT,
    ConfidenceLevel,
    ExtractedParty,
    ExtractedRuling,
    ExtractionCaseType,
    ExtractionOutcome,
    ExtractionResult,
    FieldConfidence,
)

# ---------------------------------------------------------------------------
# ConfidenceLevel enum
# ---------------------------------------------------------------------------


class TestConfidenceLevel:
    """Tests for the ConfidenceLevel StrEnum."""

    def test_values(self) -> None:
        assert ConfidenceLevel.HIGH == "high"
        assert ConfidenceLevel.MEDIUM == "medium"
        assert ConfidenceLevel.LOW == "low"

    def test_from_string(self) -> None:
        assert ConfidenceLevel("high") == ConfidenceLevel.HIGH
        assert ConfidenceLevel("medium") == ConfidenceLevel.MEDIUM
        assert ConfidenceLevel("low") == ConfidenceLevel.LOW

    def test_invalid_value_raises(self) -> None:
        with pytest.raises(ValueError):
            ConfidenceLevel("invalid")


# ---------------------------------------------------------------------------
# ExtractionOutcome enum
# ---------------------------------------------------------------------------


class TestExtractionOutcome:
    """Tests for the ExtractionOutcome StrEnum."""

    def test_all_values_present(self) -> None:
        expected = {
            "granted",
            "denied",
            "granted_in_part",
            "denied_in_part",
            "moot",
            "continued",
            "off_calendar",
            "submitted",
            "other",
        }
        actual = {v.value for v in ExtractionOutcome}
        assert actual == expected

    def test_from_string(self) -> None:
        assert ExtractionOutcome("granted") == ExtractionOutcome.GRANTED
        assert ExtractionOutcome("off_calendar") == ExtractionOutcome.OFF_CALENDAR


# ---------------------------------------------------------------------------
# ExtractionCaseType enum
# ---------------------------------------------------------------------------


class TestExtractionCaseType:
    """Tests for the ExtractionCaseType StrEnum."""

    def test_all_values_present(self) -> None:
        expected = {
            "civil",
            "criminal",
            "family",
            "probate",
            "small_claims",
            "juvenile",
            "traffic",
            "other",
        }
        actual = {v.value for v in ExtractionCaseType}
        assert actual == expected


# ---------------------------------------------------------------------------
# FieldConfidence
# ---------------------------------------------------------------------------


class TestFieldConfidence:
    """Tests for the FieldConfidence Pydantic model."""

    def test_defaults_all_high(self) -> None:
        fc = FieldConfidence()
        assert fc.case_number == ConfidenceLevel.HIGH
        assert fc.case_title == ConfidenceLevel.HIGH
        assert fc.parties == ConfidenceLevel.HIGH
        assert fc.judge == ConfidenceLevel.HIGH
        assert fc.ruling_text == ConfidenceLevel.HIGH
        assert fc.outcome == ConfidenceLevel.HIGH

    def test_custom_values(self) -> None:
        fc = FieldConfidence(
            case_number=ConfidenceLevel.LOW,
            judge=ConfidenceLevel.MEDIUM,
        )
        assert fc.case_number == ConfidenceLevel.LOW
        assert fc.judge == ConfidenceLevel.MEDIUM
        # Others should still be HIGH (default)
        assert fc.case_title == ConfidenceLevel.HIGH

    def test_serialization_round_trip(self) -> None:
        fc = FieldConfidence(
            case_number=ConfidenceLevel.HIGH,
            case_title=ConfidenceLevel.MEDIUM,
            parties=ConfidenceLevel.LOW,
            judge=ConfidenceLevel.HIGH,
            ruling_text=ConfidenceLevel.HIGH,
            outcome=ConfidenceLevel.MEDIUM,
        )
        dumped = fc.model_dump()
        restored = FieldConfidence(**dumped)
        assert restored == fc

    def test_json_round_trip(self) -> None:
        fc = FieldConfidence(judge=ConfidenceLevel.LOW)
        json_str = fc.model_dump_json()
        restored = FieldConfidence.model_validate_json(json_str)
        assert restored == fc


# ---------------------------------------------------------------------------
# ExtractedParty
# ---------------------------------------------------------------------------


class TestExtractedParty:
    """Tests for the ExtractedParty Pydantic model."""

    def test_basic_construction(self) -> None:
        party = ExtractedParty(
            name="John Smith",
            role="plaintiff",
            confidence=ConfidenceLevel.HIGH,
        )
        assert party.name == "John Smith"
        assert party.role == "plaintiff"
        assert party.confidence == ConfidenceLevel.HIGH

    def test_default_confidence(self) -> None:
        party = ExtractedParty(name="Jane Doe", role="defendant")
        assert party.confidence == ConfidenceLevel.HIGH

    def test_serialization(self) -> None:
        party = ExtractedParty(
            name="Acme Corp",
            role="defendant",
            confidence=ConfidenceLevel.MEDIUM,
        )
        dumped = party.model_dump()
        assert dumped == {
            "name": "Acme Corp",
            "role": "defendant",
            "confidence": "medium",
        }

    def test_deserialization_from_dict(self) -> None:
        data = {"name": "Bob", "role": "plaintiff", "confidence": "low"}
        party = ExtractedParty(**data)
        assert party.confidence == ConfidenceLevel.LOW


# ---------------------------------------------------------------------------
# ExtractedRuling
# ---------------------------------------------------------------------------


class TestExtractedRuling:
    """Tests for the ExtractedRuling Pydantic model."""

    def test_full_construction(self) -> None:
        ruling = ExtractedRuling(
            extracted_case_number="22SMCV01940",
            extracted_case_title="Hernandez v. Pacific Coast Properties LLC",
            extracted_parties=[
                ExtractedParty(name="Hernandez", role="plaintiff"),
                ExtractedParty(
                    name="Pacific Coast Properties LLC",
                    role="defendant",
                ),
            ],
            extracted_judge_name="Rodriguez",
            department="205",
            motion_type="msj",
            outcome=ExtractionOutcome.GRANTED,
            ruling_text="The motion is GRANTED...",
            hearing_date="2026-03-03",
            case_type=ExtractionCaseType.CIVIL,
            confidence=FieldConfidence(
                case_number=ConfidenceLevel.HIGH,
                judge=ConfidenceLevel.MEDIUM,
            ),
        )
        assert ruling.extracted_case_number == "22SMCV01940"
        assert ruling.outcome == ExtractionOutcome.GRANTED
        assert len(ruling.extracted_parties) == 2
        assert ruling.confidence.judge == ConfidenceLevel.MEDIUM

    def test_minimal_construction(self) -> None:
        """All fields are optional except confidence (has defaults)."""
        ruling = ExtractedRuling()
        assert ruling.extracted_case_number is None
        assert ruling.extracted_case_title is None
        assert ruling.extracted_parties == []
        assert ruling.outcome is None
        assert ruling.confidence.case_number == ConfidenceLevel.HIGH

    def test_json_serialization_round_trip(self) -> None:
        ruling = ExtractedRuling(
            extracted_case_number="CVPS2306157",
            extracted_case_title="Garcia v. State Farm Insurance",
            outcome=ExtractionOutcome.GRANTED,
            motion_type="motion_to_compel",
            case_type=ExtractionCaseType.CIVIL,
            extracted_parties=[
                ExtractedParty(name="Garcia", role="plaintiff"),
            ],
            confidence=FieldConfidence(outcome=ConfidenceLevel.HIGH),
        )
        json_str = ruling.model_dump_json()
        restored = ExtractedRuling.model_validate_json(json_str)
        assert restored.extracted_case_number == "CVPS2306157"
        assert restored.outcome == ExtractionOutcome.GRANTED
        assert len(restored.extracted_parties) == 1

    def test_invalid_outcome_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ExtractedRuling(outcome="invalid_outcome")

    def test_invalid_case_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ExtractedRuling(case_type="invalid_type")

    def test_extracted_ruling_entry_number_field(self) -> None:
        """entry_number field exists, serializes correctly, and defaults to None."""
        # Instantiation with entry_number set
        ruling = ExtractedRuling(entry_number=5)
        assert ruling.entry_number == 5

        # Round-trip via model_dump / model_validate
        dumped = ruling.model_dump()
        assert dumped["entry_number"] == 5
        restored = ExtractedRuling.model_validate(dumped)
        assert restored.entry_number == 5

        # Round-trip via JSON serialization
        json_str = ruling.model_dump_json()
        restored_json = ExtractedRuling.model_validate_json(json_str)
        assert restored_json.entry_number == 5

        # Legacy serialized rows that lack the field default to None
        legacy_dict = {
            "extracted_case_number": "2024-00001",
            "ruling_text": "The motion is GRANTED.",
        }
        legacy_ruling = ExtractedRuling.model_validate(legacy_dict)
        assert legacy_ruling.entry_number is None

        # Default is None when not specified
        default_ruling = ExtractedRuling()
        assert default_ruling.entry_number is None

    def test_distinguishes_raw_from_resolved(self) -> None:
        """Verify extracted_ prefix fields exist and non-prefixed don't."""
        ruling = ExtractedRuling(
            extracted_case_number="123",
            extracted_case_title="A v. B",
            extracted_judge_name="Smith",
            extracted_parties=[],
        )
        # The extracted_ prefix fields exist
        assert ruling.extracted_case_number == "123"
        assert ruling.extracted_case_title == "A v. B"
        assert ruling.extracted_judge_name == "Smith"
        # No non-prefixed aliases for these fields
        assert not hasattr(ruling, "case_number")
        assert not hasattr(ruling, "case_title")


# ---------------------------------------------------------------------------
# ExtractionResult (top-level)
# ---------------------------------------------------------------------------


class TestExtractionResult:
    """Tests for the ExtractionResult Pydantic model."""

    def test_full_construction(self) -> None:
        result = ExtractionResult(
            extracted_judge_name="Arthur Hester III",
            hearing_date="2026-03-02",
            department="PS1",
            rulings=[
                ExtractedRuling(
                    extracted_case_number="CVPS2306157",
                    extracted_case_title="Garcia v. State Farm Insurance",
                    outcome=ExtractionOutcome.GRANTED,
                ),
                ExtractedRuling(
                    extracted_case_number="CVPS2400892",
                    extracted_case_title="Thompson v. City of Palm Springs",
                    outcome=ExtractionOutcome.OTHER,
                ),
            ],
        )
        assert result.extracted_judge_name == "Arthur Hester III"
        assert result.department == "PS1"
        assert len(result.rulings) == 2

    def test_minimal_construction(self) -> None:
        result = ExtractionResult()
        assert result.extracted_judge_name is None
        assert result.hearing_date is None
        assert result.department is None
        assert result.rulings == []

    def test_json_round_trip(self) -> None:
        result = ExtractionResult(
            extracted_judge_name="Gassia Apkarian",
            hearing_date="2026-02-24",
            department="C25",
            rulings=[
                ExtractedRuling(
                    extracted_case_number="2024-01393434",
                    outcome=ExtractionOutcome.DENIED,
                    confidence=FieldConfidence(judge=ConfidenceLevel.HIGH),
                ),
            ],
        )
        json_str = result.model_dump_json()
        restored = ExtractionResult.model_validate_json(json_str)
        assert restored.extracted_judge_name == "Gassia Apkarian"
        assert restored.rulings[0].extracted_case_number == "2024-01393434"
        assert restored.rulings[0].confidence.judge == ConfidenceLevel.HIGH

    def test_deserialization_from_llm_output(self) -> None:
        """Test parsing a realistic LLM JSON response into ExtractionResult."""
        llm_json = {
            "extracted_judge_name": "Arthur Hester III",
            "hearing_date": "2026-03-02",
            "department": "PS1",
            "rulings": [
                {
                    "extracted_case_number": "CVPS2306157",
                    "extracted_case_title": "Garcia v. State Farm Insurance",
                    "case_type": "civil",
                    "outcome": "granted",
                    "motion_type": "motion_to_compel",
                    "extracted_parties": [
                        {"name": "Garcia", "role": "plaintiff", "confidence": "high"},
                        {
                            "name": "State Farm Insurance",
                            "role": "defendant",
                            "confidence": "high",
                        },
                    ],
                    "confidence": {
                        "case_number": "high",
                        "case_title": "high",
                        "parties": "high",
                        "judge": "high",
                        "ruling_text": "high",
                        "outcome": "high",
                    },
                },
            ],
        }
        result = ExtractionResult.model_validate(llm_json)
        assert result.extracted_judge_name == "Arthur Hester III"
        assert result.department == "PS1"
        assert len(result.rulings) == 1
        ruling = result.rulings[0]
        assert ruling.extracted_case_number == "CVPS2306157"
        assert ruling.outcome == ExtractionOutcome.GRANTED
        assert len(ruling.extracted_parties) == 2
        assert ruling.extracted_parties[0].name == "Garcia"
        assert ruling.confidence.case_number == ConfidenceLevel.HIGH


# ---------------------------------------------------------------------------
# JSON schema generation
# ---------------------------------------------------------------------------


class TestJsonSchemaGeneration:
    """Tests for model_json_schema() output."""

    def test_extraction_result_schema(self) -> None:
        schema = ExtractionResult.model_json_schema()
        assert schema["type"] == "object"
        assert "extracted_judge_name" in schema["properties"]
        assert "rulings" in schema["properties"]

    def test_extracted_ruling_schema(self) -> None:
        schema = ExtractedRuling.model_json_schema()
        props = schema["properties"]
        assert "extracted_case_number" in props
        assert "extracted_case_title" in props
        assert "extracted_parties" in props
        assert "extracted_judge_name" in props
        assert "confidence" in props
        assert "outcome" in props
        assert "case_type" in props

    def test_schema_is_valid_json(self) -> None:
        """Verify the schema can be serialized to a JSON string."""
        schema = ExtractionResult.model_json_schema()
        json_str = json.dumps(schema)
        reparsed = json.loads(json_str)
        assert reparsed["type"] == "object"

    def test_field_confidence_schema(self) -> None:
        schema = FieldConfidence.model_json_schema()
        props = schema["properties"]
        assert "case_number" in props
        assert "case_title" in props
        assert "parties" in props
        assert "judge" in props
        assert "ruling_text" in props
        assert "outcome" in props


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------


class TestExtractionSystemPrompt:
    """Tests for the co-located system prompt."""

    def test_prompt_is_non_empty_string(self) -> None:
        assert isinstance(EXTRACTION_SYSTEM_PROMPT, str)
        assert len(EXTRACTION_SYSTEM_PROMPT) > 100

    def test_prompt_includes_confidence_instructions(self) -> None:
        assert "confidence" in EXTRACTION_SYSTEM_PROMPT.lower()

    def test_prompt_includes_few_shot_examples(self) -> None:
        # Should have at least 3 examples (LA, OC, Riverside)
        assert "Example 1" in EXTRACTION_SYSTEM_PROMPT
        assert "Example 2" in EXTRACTION_SYSTEM_PROMPT
        assert "Example 3" in EXTRACTION_SYSTEM_PROMPT

    def test_prompt_includes_extracted_prefix_fields(self) -> None:
        """The prompt should use extracted_ prefix field names."""
        assert "extracted_case_number" in EXTRACTION_SYSTEM_PROMPT
        assert "extracted_case_title" in EXTRACTION_SYSTEM_PROMPT
        assert "extracted_judge_name" in EXTRACTION_SYSTEM_PROMPT
        assert "extracted_parties" in EXTRACTION_SYSTEM_PROMPT

    def test_prompt_includes_outcome_taxonomy(self) -> None:
        assert "granted" in EXTRACTION_SYSTEM_PROMPT
        assert "denied" in EXTRACTION_SYSTEM_PROMPT
        assert "off_calendar" in EXTRACTION_SYSTEM_PROMPT
        assert "submitted" in EXTRACTION_SYSTEM_PROMPT

    def test_prompt_includes_case_type_taxonomy(self) -> None:
        assert "civil" in EXTRACTION_SYSTEM_PROMPT
        assert "criminal" in EXTRACTION_SYSTEM_PROMPT
        assert "probate" in EXTRACTION_SYSTEM_PROMPT

    def test_prompt_includes_verbatim_instruction(self) -> None:
        """Ruling text must be preserved verbatim."""
        assert "verbatim" in EXTRACTION_SYSTEM_PROMPT.lower()
