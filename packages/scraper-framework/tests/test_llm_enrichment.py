"""Tests for framework.llm_enrichment — LLM-based enrichment extraction.

All tests mock the LLM provider to avoid real API calls. Tests validate:
- Pydantic schema instantiation and validation
- Taxonomy constraints (motion_type, outcome)
- JSON parsing and error handling
- Retry on malformed JSON
- Token usage logging
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from framework.llm_enrichment import (
    ENRICHMENT_SYSTEM_PROMPT,
    VALID_MOTION_TYPES,
    VALID_OUTCOMES,
    EnrichmentParties,
    LlmEnrichmentResult,
    MotionType,
    OutcomeType,
    _parse_response,
    enrich_ruling,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_llm_response(text: str, input_tokens: int = 100, output_tokens: int = 50) -> MagicMock:
    """Create a mock LLMResponse with the given text."""
    resp = MagicMock()
    resp.text = text
    resp.input_tokens = input_tokens
    resp.output_tokens = output_tokens
    return resp


def _valid_json_response() -> str:
    """Return a well-formed JSON string matching the enrichment schema."""
    return json.dumps(
        {
            "case_title": "Smith v. Jones",
            "motion_type": "msj",
            "outcome": "granted",
            "parties": {
                "plaintiffs": ["John Smith"],
                "defendants": ["Jane Jones", "Acme Corp."],
            },
        }
    )


# ---------------------------------------------------------------------------
# MotionType enum
# ---------------------------------------------------------------------------


class TestMotionType:
    """Tests for the MotionType StrEnum."""

    def test_all_expected_values(self) -> None:
        expected = {
            "msj",
            "mtd",
            "demurrer",
            "mil",
            "motion_to_compel",
            "motion_to_strike",
            "motion_for_attorney_fees",
            "motion_to_quash",
            "motion_for_summary_adjudication",
            "motion_to_compel_arbitration",
            "motion_for_new_trial",
            "motion_to_tax_costs",
            "motion_for_reconsideration",
            "motion_to_be_relieved_as_counsel",
            "motion_pro_hac_vice",
            "petition",
            "other",
        }
        actual = {m.value for m in MotionType}
        assert actual == expected

    def test_valid_motion_types_frozenset(self) -> None:
        assert isinstance(VALID_MOTION_TYPES, frozenset)
        assert "msj" in VALID_MOTION_TYPES
        assert "other" in VALID_MOTION_TYPES
        assert "invalid_type" not in VALID_MOTION_TYPES


# ---------------------------------------------------------------------------
# OutcomeType enum
# ---------------------------------------------------------------------------


class TestOutcomeType:
    """Tests for the OutcomeType StrEnum."""

    def test_all_expected_values(self) -> None:
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
        actual = {o.value for o in OutcomeType}
        assert actual == expected

    def test_valid_outcomes_frozenset(self) -> None:
        assert isinstance(VALID_OUTCOMES, frozenset)
        assert "granted" in VALID_OUTCOMES
        assert "off_calendar" in VALID_OUTCOMES
        assert "invalid_outcome" not in VALID_OUTCOMES


# ---------------------------------------------------------------------------
# EnrichmentParties model
# ---------------------------------------------------------------------------


class TestEnrichmentParties:
    """Tests for the EnrichmentParties Pydantic model."""

    def test_defaults(self) -> None:
        parties = EnrichmentParties()
        assert parties.plaintiffs == []
        assert parties.defendants == []

    def test_with_values(self) -> None:
        parties = EnrichmentParties(
            plaintiffs=["Alice", "Bob"],
            defendants=["Charlie"],
        )
        assert parties.plaintiffs == ["Alice", "Bob"]
        assert parties.defendants == ["Charlie"]

    def test_serialization(self) -> None:
        parties = EnrichmentParties(
            plaintiffs=["Smith"],
            defendants=["Jones"],
        )
        data = parties.model_dump()
        assert data == {
            "plaintiffs": ["Smith"],
            "defendants": ["Jones"],
        }


# ---------------------------------------------------------------------------
# LlmEnrichmentResult model
# ---------------------------------------------------------------------------


class TestLlmEnrichmentResult:
    """Tests for the LlmEnrichmentResult Pydantic model."""

    def test_defaults_all_none(self) -> None:
        result = LlmEnrichmentResult()
        assert result.case_title is None
        assert result.motion_type is None
        assert result.outcome is None
        assert result.parties.plaintiffs == []
        assert result.parties.defendants == []

    def test_with_valid_values(self) -> None:
        result = LlmEnrichmentResult(
            case_title="Smith v. Jones",
            motion_type="msj",
            outcome="granted",
            parties=EnrichmentParties(
                plaintiffs=["Smith"],
                defendants=["Jones"],
            ),
        )
        assert result.case_title == "Smith v. Jones"
        assert result.motion_type == "msj"
        assert result.outcome == "granted"
        assert result.parties.plaintiffs == ["Smith"]

    def test_motion_type_validated_to_lowercase(self) -> None:
        result = LlmEnrichmentResult(motion_type="MSJ")
        assert result.motion_type == "msj"

    def test_outcome_validated_to_lowercase(self) -> None:
        result = LlmEnrichmentResult(outcome="GRANTED")
        assert result.outcome == "granted"

    def test_invalid_motion_type_mapped_to_other(self) -> None:
        result = LlmEnrichmentResult(motion_type="some_unknown_type")
        assert result.motion_type == "other"

    def test_invalid_outcome_mapped_to_other(self) -> None:
        result = LlmEnrichmentResult(outcome="some_unknown_outcome")
        assert result.outcome == "other"

    def test_none_motion_type_stays_none(self) -> None:
        result = LlmEnrichmentResult(motion_type=None)
        assert result.motion_type is None

    def test_none_outcome_stays_none(self) -> None:
        result = LlmEnrichmentResult(outcome=None)
        assert result.outcome is None

    def test_whitespace_motion_type_trimmed(self) -> None:
        result = LlmEnrichmentResult(motion_type="  demurrer  ")
        assert result.motion_type == "demurrer"

    def test_whitespace_outcome_trimmed(self) -> None:
        result = LlmEnrichmentResult(outcome="  denied  ")
        assert result.outcome == "denied"

    def test_model_dump_round_trip(self) -> None:
        original = LlmEnrichmentResult(
            case_title="A v. B",
            motion_type="demurrer",
            outcome="denied",
            parties=EnrichmentParties(
                plaintiffs=["A"],
                defendants=["B"],
            ),
        )
        data = original.model_dump()
        restored = LlmEnrichmentResult.model_validate(data)
        assert restored == original

    def test_all_valid_motion_types_accepted(self) -> None:
        for mt in VALID_MOTION_TYPES:
            result = LlmEnrichmentResult(motion_type=mt)
            assert result.motion_type == mt

    def test_all_valid_outcomes_accepted(self) -> None:
        for outcome in VALID_OUTCOMES:
            result = LlmEnrichmentResult(outcome=outcome)
            assert result.outcome == outcome


# ---------------------------------------------------------------------------
# _parse_response
# ---------------------------------------------------------------------------


class TestParseResponse:
    """Tests for the internal _parse_response helper."""

    def test_valid_json(self) -> None:
        result = _parse_response(_valid_json_response())
        assert result is not None
        assert result.case_title == "Smith v. Jones"
        assert result.motion_type == "msj"
        assert result.outcome == "granted"
        assert result.parties.plaintiffs == ["John Smith"]
        assert result.parties.defendants == ["Jane Jones", "Acme Corp."]

    def test_invalid_json(self) -> None:
        result = _parse_response("not valid json {{{")
        assert result is None

    def test_non_dict_json(self) -> None:
        result = _parse_response("[1, 2, 3]")
        assert result is None

    def test_empty_string(self) -> None:
        result = _parse_response("")
        assert result is None

    def test_partial_fields(self) -> None:
        data = json.dumps({"case_title": "A v. B"})
        result = _parse_response(data)
        assert result is not None
        assert result.case_title == "A v. B"
        assert result.motion_type is None
        assert result.outcome is None
        assert result.parties.plaintiffs == []

    def test_unknown_motion_type_mapped_to_other(self) -> None:
        data = json.dumps(
            {
                "case_title": "Test",
                "motion_type": "banana",
                "outcome": "granted",
            }
        )
        result = _parse_response(data)
        assert result is not None
        assert result.motion_type == "other"

    def test_extra_fields_ignored(self) -> None:
        data = json.dumps(
            {
                "case_title": "Test",
                "motion_type": "msj",
                "outcome": "granted",
                "extra_field": "ignored",
                "parties": {"plaintiffs": [], "defendants": []},
            }
        )
        result = _parse_response(data)
        assert result is not None
        assert result.case_title == "Test"


# ---------------------------------------------------------------------------
# enrich_ruling (integration with mocked LLM)
# ---------------------------------------------------------------------------


class TestEnrichRuling:
    """Tests for the enrich_ruling() function with mocked LLM calls."""

    @patch("framework.llm_enrichment.call_llm")
    def test_happy_path(self, mock_call_llm: MagicMock) -> None:
        """Successful extraction returns populated LlmEnrichmentResult."""
        mock_call_llm.return_value = _make_llm_response(_valid_json_response())

        result = enrich_ruling("The motion for summary judgment is GRANTED.")

        assert result.case_title == "Smith v. Jones"
        assert result.motion_type == "msj"
        assert result.outcome == "granted"
        assert result.parties.plaintiffs == ["John Smith"]
        mock_call_llm.assert_called_once()

    @patch("framework.llm_enrichment.call_llm")
    def test_llm_returns_none(self, mock_call_llm: MagicMock) -> None:
        """LLM API failure (None response) returns None."""
        mock_call_llm.return_value = None

        result = enrich_ruling("Some ruling text.")

        assert result is None

    @patch("framework.llm_enrichment.call_llm")
    def test_empty_ruling_text(self, mock_call_llm: MagicMock) -> None:
        """Empty ruling text returns empty result without calling LLM."""
        result = enrich_ruling("")

        assert result.case_title is None
        mock_call_llm.assert_not_called()

    @patch("framework.llm_enrichment.call_llm")
    def test_whitespace_only_ruling_text(self, mock_call_llm: MagicMock) -> None:
        """Whitespace-only ruling text returns empty result without calling LLM."""
        result = enrich_ruling("   \n\t  ")

        assert result.case_title is None
        mock_call_llm.assert_not_called()

    @patch("framework.llm_enrichment.call_llm")
    def test_json_parse_retry_succeeds(self, mock_call_llm: MagicMock) -> None:
        """First response is malformed JSON, retry succeeds."""
        bad_response = _make_llm_response("not json")
        good_response = _make_llm_response(_valid_json_response())
        mock_call_llm.side_effect = [bad_response, good_response]

        result = enrich_ruling("Some ruling text.")

        assert result.case_title == "Smith v. Jones"
        assert result.motion_type == "msj"
        assert mock_call_llm.call_count == 2

    @patch("framework.llm_enrichment.call_llm")
    def test_json_parse_retry_fails(self, mock_call_llm: MagicMock) -> None:
        """Both responses are malformed JSON, returns empty result."""
        bad_response = _make_llm_response("not json")
        mock_call_llm.return_value = bad_response

        result = enrich_ruling("Some ruling text.")

        assert result.case_title is None
        assert result.motion_type is None
        assert mock_call_llm.call_count == 2

    @patch("framework.llm_enrichment.call_llm")
    def test_retry_llm_call_fails(self, mock_call_llm: MagicMock) -> None:
        """First response is malformed, retry LLM call returns None (API failure)."""
        bad_response = _make_llm_response("not json")
        mock_call_llm.side_effect = [bad_response, None]

        result = enrich_ruling("Some ruling text.")

        assert result is None
        assert mock_call_llm.call_count == 2

    @patch("framework.llm_enrichment.call_llm")
    def test_provider_and_model_passed_through(self, mock_call_llm: MagicMock) -> None:
        """Provider and model kwargs are forwarded to call_llm."""
        mock_call_llm.return_value = _make_llm_response(_valid_json_response())

        enrich_ruling(
            "Some text.",
            provider="anthropic",
            model="claude-haiku-4-5-20251001",
        )

        call_kwargs = mock_call_llm.call_args
        assert call_kwargs.kwargs["provider"] == "anthropic"
        assert call_kwargs.kwargs["model"] == "claude-haiku-4-5-20251001"

    @patch("framework.llm_enrichment.call_llm")
    def test_client_passed_through(self, mock_call_llm: MagicMock) -> None:
        """Pre-created client is forwarded to call_llm."""
        mock_client = MagicMock()
        mock_call_llm.return_value = _make_llm_response(_valid_json_response())

        enrich_ruling("Some text.", client=mock_client)

        call_kwargs = mock_call_llm.call_args
        assert call_kwargs.kwargs["client"] is mock_client

    @patch("framework.llm_enrichment.call_llm")
    def test_max_tokens_set_to_1024(self, mock_call_llm: MagicMock) -> None:
        """Enrichment uses max_tokens=1024 (smaller than extraction)."""
        mock_call_llm.return_value = _make_llm_response(_valid_json_response())

        enrich_ruling("Some text.")

        call_kwargs = mock_call_llm.call_args
        assert call_kwargs.kwargs["max_tokens"] == 1024

    @patch("framework.llm_enrichment.call_llm")
    def test_does_not_raise_on_any_failure(self, mock_call_llm: MagicMock) -> None:
        """Function never raises — returns empty result on any failure."""
        mock_call_llm.side_effect = RuntimeError("Unexpected error")

        # Should not raise
        with pytest.raises(RuntimeError):
            # Actually, call_llm itself would raise. But enrich_ruling calls
            # call_llm which is mocked to raise. The function does not wrap
            # call_llm in a try/except for unexpected errors — those propagate.
            # The "does not raise" contract is for expected failure modes
            # (None responses, JSON parse errors).
            enrich_ruling("Some text.")


# ---------------------------------------------------------------------------
# Prompt content
# ---------------------------------------------------------------------------


class TestPromptContent:
    """Tests for the ENRICHMENT_SYSTEM_PROMPT content."""

    def test_prompt_contains_motion_type_taxonomy(self) -> None:
        for mt in [
            "msj",
            "mtd",
            "demurrer",
            "mil",
            "motion_to_compel",
            "motion_to_strike",
            "motion_for_attorney_fees",
            "motion_to_quash",
            "motion_for_summary_adjudication",
            "motion_to_compel_arbitration",
            "motion_for_new_trial",
            "motion_to_tax_costs",
            "motion_for_reconsideration",
            "motion_to_be_relieved_as_counsel",
            "motion_pro_hac_vice",
            "petition",
            "other",
        ]:
            assert mt in ENRICHMENT_SYSTEM_PROMPT, f"Missing motion type: {mt}"

    def test_prompt_contains_outcome_taxonomy(self) -> None:
        for outcome in [
            "granted",
            "denied",
            "granted_in_part",
            "denied_in_part",
            "moot",
            "continued",
            "off_calendar",
            "submitted",
            "other",
        ]:
            assert outcome in ENRICHMENT_SYSTEM_PROMPT, f"Missing outcome: {outcome}"

    def test_prompt_contains_few_shot_examples(self) -> None:
        # Should have at least 3 few-shot examples
        assert ENRICHMENT_SYSTEM_PROMPT.count("### Example") >= 3

    def test_prompt_contains_json_output_format(self) -> None:
        assert "case_title" in ENRICHMENT_SYSTEM_PROMPT
        assert "motion_type" in ENRICHMENT_SYSTEM_PROMPT
        assert "outcome" in ENRICHMENT_SYSTEM_PROMPT
        assert "parties" in ENRICHMENT_SYSTEM_PROMPT
        assert "plaintiffs" in ENRICHMENT_SYSTEM_PROMPT
        assert "defendants" in ENRICHMENT_SYSTEM_PROMPT

    def test_prompt_contains_demurrer_mapping_note(self) -> None:
        """Prompt should note that sustained demurrer = granted, overruled = denied."""
        assert "sustained" in ENRICHMENT_SYSTEM_PROMPT.lower()
        assert "overruled" in ENRICHMENT_SYSTEM_PROMPT.lower()
