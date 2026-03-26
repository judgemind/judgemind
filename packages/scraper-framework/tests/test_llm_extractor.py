"""Tests for framework.llm_extractor — LlmExtractor class.

Validates extraction from mocked API responses, retry behavior on
transient errors, chunking, metadata overrides, and token usage logging.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import anthropic
import pytest

from framework.llm_extractor import (
    LlmExtractor,
    TokenUsage,
    _force_split,
    _normalize_case_number,
    _parse_confidence,
    _parse_single_ruling,
)
from framework.llm_schema import (
    ConfidenceLevel,
    ExtractionCaseType,
    ExtractionOutcome,
    FieldConfidence,
)

# ---------------------------------------------------------------------------
# Fixtures: mock Anthropic client and responses
# ---------------------------------------------------------------------------


def _make_response(text: str, input_tokens: int = 100, output_tokens: int = 50) -> MagicMock:
    """Create a mock Anthropic messages.create() response."""
    content_block = MagicMock()
    content_block.text = text

    usage = MagicMock()
    usage.input_tokens = input_tokens
    usage.output_tokens = output_tokens

    response = MagicMock()
    response.content = [content_block]
    response.usage = usage
    return response


SINGLE_RULING_JSON = json.dumps(
    {
        "extracted_judge_name": "Gassia Apkarian",
        "hearing_date": "2026-02-24",
        "department": "C25",
        "rulings": [
            {
                "extracted_case_number": "2024-01393434",
                "extracted_case_title": "Martinez v. ABC Manufacturing Inc.",
                "case_type": "civil",
                "outcome": "denied",
                "motion_type": "msj_partial",
                "ruling_text": "The motion is DENIED.",
                "hearing_date": "2026-02-24",
                "extracted_parties": [
                    {"name": "Martinez", "role": "plaintiff", "confidence": "high"},
                    {
                        "name": "ABC Manufacturing Inc.",
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
            }
        ],
    }
)


MULTI_RULING_JSON = json.dumps(
    {
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
                "ruling_text": "The motion is GRANTED.",
                "hearing_date": "2026-03-02",
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
            {
                "extracted_case_number": "CVPS2400892",
                "extracted_case_title": "Thompson v. City of Palm Springs",
                "case_type": "civil",
                "outcome": "denied",
                "motion_type": "demurrer",
                "ruling_text": "The demurrer is OVERRULED.",
                "hearing_date": "2026-03-02",
                "extracted_parties": [
                    {"name": "Thompson", "role": "plaintiff", "confidence": "high"},
                    {
                        "name": "City of Palm Springs",
                        "role": "defendant",
                        "confidence": "medium",
                    },
                ],
                "confidence": {
                    "case_number": "high",
                    "case_title": "high",
                    "parties": "medium",
                    "judge": "high",
                    "ruling_text": "high",
                    "outcome": "medium",
                },
            },
        ],
    }
)


@pytest.fixture
def mock_client() -> MagicMock:
    """Return a mock Anthropic client."""
    client = MagicMock(spec=anthropic.Anthropic)
    client.messages = MagicMock()
    return client


@pytest.fixture
def extractor(mock_client: MagicMock) -> LlmExtractor:
    """Return an LlmExtractor with a mocked Anthropic client."""
    with patch.object(anthropic, "Anthropic", return_value=mock_client):
        ext = LlmExtractor(api_key="test-key")
    ext._client = mock_client
    return ext


# ---------------------------------------------------------------------------
# Basic extraction
# ---------------------------------------------------------------------------


class TestExtractBasic:
    """Tests for basic extraction functionality."""

    def test_empty_text_returns_empty_list(self, extractor: LlmExtractor) -> None:
        assert extractor.extract("") == []
        assert extractor.extract("   ") == []
        assert extractor.extract(None) == []  # type: ignore[arg-type]

    def test_single_ruling_extraction(
        self, extractor: LlmExtractor, mock_client: MagicMock
    ) -> None:
        mock_client.messages.create.return_value = _make_response(SINGLE_RULING_JSON)

        rulings = extractor.extract("Case No. 2024-01393434\nMartinez v. ABC Manufacturing")

        assert len(rulings) == 1
        ruling = rulings[0]
        assert ruling.extracted_case_number == "2024-01393434"
        assert ruling.extracted_case_title == "Martinez v. ABC Manufacturing Inc."
        assert ruling.outcome == ExtractionOutcome.DENIED
        assert ruling.motion_type == "msj_partial"
        assert ruling.case_type == ExtractionCaseType.CIVIL
        assert ruling.ruling_text == "The motion is DENIED."
        assert len(ruling.extracted_parties) == 2
        assert ruling.extracted_parties[0].name == "Martinez"
        assert ruling.extracted_parties[0].role == "plaintiff"
        assert ruling.confidence.case_number == ConfidenceLevel.HIGH

    def test_multi_ruling_extraction(self, extractor: LlmExtractor, mock_client: MagicMock) -> None:
        mock_client.messages.create.return_value = _make_response(MULTI_RULING_JSON)

        rulings = extractor.extract("Tentative Rulings for March 2, 2026...")

        assert len(rulings) == 2
        assert rulings[0].extracted_case_number == "CVPS2306157"
        assert rulings[0].outcome == ExtractionOutcome.GRANTED
        assert rulings[1].extracted_case_number == "CVPS2400892"
        assert rulings[1].outcome == ExtractionOutcome.DENIED

    def test_api_failure_returns_empty_list(
        self, extractor: LlmExtractor, mock_client: MagicMock
    ) -> None:
        mock_client.messages.create.side_effect = anthropic.APIConnectionError(request=MagicMock())
        rulings = extractor.extract("Some text")
        assert rulings == []


# ---------------------------------------------------------------------------
# Retry behavior
# ---------------------------------------------------------------------------


class TestRetryBehavior:
    """Tests for retry on transient API errors."""

    def test_retry_on_rate_limit(self, extractor: LlmExtractor, mock_client: MagicMock) -> None:
        """Should retry on 429 and succeed on second attempt."""
        rate_limit_response = MagicMock()
        rate_limit_response.status_code = 429
        rate_limit_response.headers = {}

        mock_client.messages.create.side_effect = [
            anthropic.RateLimitError(
                message="rate limited",
                response=rate_limit_response,
                body=None,
            ),
            _make_response(SINGLE_RULING_JSON),
        ]
        extractor._base_delay = 0.01  # Speed up test

        rulings = extractor.extract("Some text")
        assert len(rulings) == 1
        assert mock_client.messages.create.call_count == 2

    def test_retry_on_server_error(self, extractor: LlmExtractor, mock_client: MagicMock) -> None:
        """Should retry on 500 and succeed on second attempt."""
        server_error_response = MagicMock()
        server_error_response.status_code = 500
        server_error_response.headers = {}

        mock_client.messages.create.side_effect = [
            anthropic.InternalServerError(
                message="server error",
                response=server_error_response,
                body=None,
            ),
            _make_response(SINGLE_RULING_JSON),
        ]
        extractor._base_delay = 0.01

        rulings = extractor.extract("Some text")
        assert len(rulings) == 1
        assert mock_client.messages.create.call_count == 2

    def test_retry_on_529_overloaded(self, extractor: LlmExtractor, mock_client: MagicMock) -> None:
        """Should retry on 529 (Anthropic overloaded)."""
        overloaded_response = MagicMock()
        overloaded_response.status_code = 529
        overloaded_response.headers = {}

        mock_client.messages.create.side_effect = [
            anthropic.APIStatusError(
                message="overloaded",
                response=overloaded_response,
                body=None,
            ),
            _make_response(SINGLE_RULING_JSON),
        ]
        extractor._base_delay = 0.01

        rulings = extractor.extract("Some text")
        assert len(rulings) == 1

    def test_exhausts_retries_returns_empty(
        self, extractor: LlmExtractor, mock_client: MagicMock
    ) -> None:
        """Should return empty list after exhausting all retries."""
        rate_limit_response = MagicMock()
        rate_limit_response.status_code = 429
        rate_limit_response.headers = {}

        mock_client.messages.create.side_effect = anthropic.RateLimitError(
            message="rate limited",
            response=rate_limit_response,
            body=None,
        )
        extractor._base_delay = 0.01
        extractor._max_retries = 2

        rulings = extractor.extract("Some text")
        assert rulings == []
        assert mock_client.messages.create.call_count == 2

    def test_non_retryable_error_no_retry(
        self, extractor: LlmExtractor, mock_client: MagicMock
    ) -> None:
        """Non-retryable status codes should not be retried."""
        bad_request_response = MagicMock()
        bad_request_response.status_code = 400
        bad_request_response.headers = {}

        mock_client.messages.create.side_effect = anthropic.APIStatusError(
            message="bad request",
            response=bad_request_response,
            body=None,
        )

        rulings = extractor.extract("Some text")
        assert rulings == []
        assert mock_client.messages.create.call_count == 1


# ---------------------------------------------------------------------------
# Metadata overrides
# ---------------------------------------------------------------------------


class TestMetadata:
    """Tests for metadata override behavior."""

    def test_metadata_overrides_llm_fields(
        self, extractor: LlmExtractor, mock_client: MagicMock
    ) -> None:
        mock_client.messages.create.return_value = _make_response(SINGLE_RULING_JSON)

        rulings = extractor.extract(
            "Some text",
            metadata={"judge_name": "Override Judge", "department": "D99"},
        )

        assert len(rulings) == 1
        # The ExtractionResult top-level fields are overridden;
        # individual ruling fields remain as extracted.

    def test_metadata_included_in_user_message(
        self, extractor: LlmExtractor, mock_client: MagicMock
    ) -> None:
        mock_client.messages.create.return_value = _make_response(SINGLE_RULING_JSON)

        extractor.extract(
            "Some text",
            metadata={
                "judge_name": "Override Judge",
                "department": "D99",
                "hearing_date": "2026-01-15",
            },
        )

        call_args = mock_client.messages.create.call_args
        user_message = call_args.kwargs["messages"][0]["content"]
        assert "Override Judge" in user_message
        assert "D99" in user_message
        assert "2026-01-15" in user_message


# ---------------------------------------------------------------------------
# Configurable model
# ---------------------------------------------------------------------------


class TestConfigurableModel:
    """Tests for configurable model selection."""

    def test_default_model(self) -> None:
        with patch.object(anthropic, "Anthropic"):
            ext = LlmExtractor(api_key="test-key")
        from judgemind_config import DEFAULT_HAIKU_MODEL

        assert ext._model == DEFAULT_HAIKU_MODEL

    def test_custom_model(self) -> None:
        with patch.object(anthropic, "Anthropic"):
            ext = LlmExtractor(api_key="test-key", model="claude-sonnet-4-20250514")
        assert ext._model == "claude-sonnet-4-20250514"

    def test_model_passed_to_api(self, mock_client: MagicMock) -> None:
        with patch.object(anthropic, "Anthropic", return_value=mock_client):
            ext = LlmExtractor(api_key="test-key", model="claude-sonnet-4-20250514")
        ext._client = mock_client
        mock_client.messages.create.return_value = _make_response(SINGLE_RULING_JSON)

        ext.extract("Some text")

        call_args = mock_client.messages.create.call_args
        assert call_args.kwargs["model"] == "claude-sonnet-4-20250514"


# ---------------------------------------------------------------------------
# Token usage logging
# ---------------------------------------------------------------------------


class TestTokenUsage:
    """Tests for token usage tracking and logging."""

    def test_single_call_usage(self, extractor: LlmExtractor, mock_client: MagicMock) -> None:
        mock_client.messages.create.return_value = _make_response(
            SINGLE_RULING_JSON, input_tokens=500, output_tokens=200
        )

        with patch("framework.llm_extractor.logger") as mock_logger:
            extractor.extract("Some text")

            # Find the token_usage log call
            usage_calls = [
                c
                for c in mock_logger.info.call_args_list
                if c.args and c.args[0] == "llm_extractor.token_usage"
            ]
            assert len(usage_calls) == 1
            assert usage_calls[0].kwargs["input_tokens"] == 500
            assert usage_calls[0].kwargs["output_tokens"] == 200
            assert usage_calls[0].kwargs["api_calls"] == 1

    def test_token_usage_dataclass(self) -> None:
        usage = TokenUsage()
        assert usage.input_tokens == 0
        assert usage.output_tokens == 0
        assert usage.api_calls == 0

        usage.input_tokens += 100
        usage.output_tokens += 50
        usage.api_calls += 1
        assert usage.input_tokens == 100
        assert usage.output_tokens == 50
        assert usage.api_calls == 1


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------


class TestChunking:
    """Tests for document chunking behavior."""

    def test_short_text_no_chunking(self, extractor: LlmExtractor) -> None:
        chunks = extractor._split_into_chunks("Short text.")
        assert len(chunks) == 1

    def test_large_text_with_page_breaks(self, extractor: LlmExtractor) -> None:
        extractor._max_chars_per_chunk = 100
        # Create text with page breaks that total more than max_chars.
        text = "A" * 80 + "\f" + "B" * 80
        chunks = extractor._split_into_chunks(text)
        assert len(chunks) >= 2

    def test_large_text_with_case_boundaries(self, extractor: LlmExtractor) -> None:
        extractor._max_chars_per_chunk = 100
        text = "X" * 60 + "\nCase Number: 123\n" + "Y" * 60
        chunks = extractor._split_into_chunks(text)
        assert len(chunks) >= 2

    def test_force_split_on_wall_of_text(self, extractor: LlmExtractor) -> None:
        extractor._max_chars_per_chunk = 50
        text = "A" * 120  # No natural boundaries
        chunks = extractor._split_into_chunks(text)
        assert len(chunks) >= 2
        assert all(len(c) <= 50 for c in chunks)

    def test_chunked_extraction_deduplicates(
        self, extractor: LlmExtractor, mock_client: MagicMock
    ) -> None:
        """When chunks produce overlapping rulings, they should be deduplicated."""
        extractor._max_chars_per_chunk = 50

        # Both chunks return the same case number.
        response_json = json.dumps(
            {
                "extracted_judge_name": "Smith",
                "hearing_date": "2026-01-01",
                "department": "A1",
                "rulings": [
                    {
                        "extracted_case_number": "12345",
                        "extracted_case_title": "A v. B",
                        "outcome": "granted",
                    }
                ],
            }
        )
        mock_client.messages.create.return_value = _make_response(response_json)

        # Create text that forces 2 chunks.
        text = "A" * 80
        rulings = extractor.extract(text)

        # Should deduplicate to 1 ruling.
        assert len(rulings) == 1
        assert rulings[0].extracted_case_number == "12345"


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


class TestResponseParsing:
    """Tests for response parsing edge cases."""

    def test_markdown_code_fence_stripped(self, extractor: LlmExtractor) -> None:
        fenced = f"```json\n{SINGLE_RULING_JSON}\n```"
        result = extractor._parse_response(fenced, metadata=None)
        assert result is not None
        assert len(result.rulings) == 1

    def test_invalid_json_returns_none(self, extractor: LlmExtractor) -> None:
        result = extractor._parse_response("not valid json", metadata=None)
        assert result is None

    def test_legacy_field_names_supported(self, extractor: LlmExtractor) -> None:
        """Should handle both extracted_ prefix and legacy field names."""
        legacy_json = json.dumps(
            {
                "judge_name": "Legacy Judge",
                "hearing_date": "2026-01-01",
                "department": "A1",
                "rulings": [
                    {
                        "case_number": "12345",
                        "case_title": "X v. Y",
                        "outcome": "granted",
                        "parties": [
                            {"name": "X", "role": "plaintiff"},
                            {"name": "Y", "role": "defendant"},
                        ],
                    }
                ],
            }
        )
        result = extractor._parse_response(legacy_json, metadata=None)
        assert result is not None
        assert result.extracted_judge_name == "Legacy Judge"
        assert len(result.rulings) == 1
        assert result.rulings[0].extracted_case_number == "12345"
        assert result.rulings[0].extracted_case_title == "X v. Y"
        assert len(result.rulings[0].extracted_parties) == 2

    def test_invalid_outcome_mapped_to_other(self) -> None:
        ruling = _parse_single_ruling({"outcome": "invalid_value"})
        assert ruling.outcome == ExtractionOutcome.OTHER

    def test_invalid_case_type_mapped_to_other(self) -> None:
        ruling = _parse_single_ruling({"case_type": "unknown_type"})
        assert ruling.case_type == ExtractionCaseType.OTHER

    def test_party_name_too_long_rejected(self) -> None:
        ruling = _parse_single_ruling(
            {
                "extracted_parties": [
                    {"name": "A" * 300, "role": "plaintiff"},
                    {"name": "Valid Name", "role": "defendant"},
                ]
            }
        )
        assert len(ruling.extracted_parties) == 1
        assert ruling.extracted_parties[0].name == "Valid Name"

    def test_party_name_with_newline_rejected(self) -> None:
        ruling = _parse_single_ruling(
            {
                "extracted_parties": [
                    {"name": "Bad\nName", "role": "plaintiff"},
                ]
            }
        )
        assert len(ruling.extracted_parties) == 0


# ---------------------------------------------------------------------------
# Case number normalization
# ---------------------------------------------------------------------------


class TestCaseNumberNormalization:
    """Tests for case number normalization."""

    def test_oc_prefix_stripped(self) -> None:
        assert _normalize_case_number("30-2024-01393434") == "2024-01393434"

    def test_no_prefix_unchanged(self) -> None:
        assert _normalize_case_number("22SMCV01940") == "22SMCV01940"

    def test_whitespace_stripped(self) -> None:
        assert _normalize_case_number("  22SMCV01940  ") == "22SMCV01940"


# ---------------------------------------------------------------------------
# Confidence parsing
# ---------------------------------------------------------------------------


class TestConfidenceParsing:
    """Tests for confidence score parsing."""

    def test_valid_confidence(self) -> None:
        raw = {
            "case_number": "high",
            "case_title": "medium",
            "parties": "low",
            "judge": "high",
            "ruling_text": "medium",
            "outcome": "high",
        }
        fc = _parse_confidence(raw)
        assert fc.case_number == ConfidenceLevel.HIGH
        assert fc.case_title == ConfidenceLevel.MEDIUM
        assert fc.parties == ConfidenceLevel.LOW

    def test_missing_fields_default_high(self) -> None:
        fc = _parse_confidence({})
        assert fc.case_number == ConfidenceLevel.HIGH
        assert fc.judge == ConfidenceLevel.HIGH

    def test_invalid_values_default_high(self) -> None:
        fc = _parse_confidence({"case_number": "invalid"})
        assert fc.case_number == ConfidenceLevel.HIGH

    def test_none_input(self) -> None:
        fc = _parse_confidence(None)
        assert fc == FieldConfidence()


# ---------------------------------------------------------------------------
# Force split
# ---------------------------------------------------------------------------


class TestForceSplit:
    """Tests for the _force_split helper."""

    def test_splits_correctly(self) -> None:
        chunks = _force_split("A" * 200, 100)
        assert len(chunks) >= 2
        assert all(len(c) <= 100 for c in chunks)

    def test_single_chunk_if_small(self) -> None:
        chunks = _force_split("Hello", 100)
        assert len(chunks) == 1
        assert chunks[0] == "Hello"


# ---------------------------------------------------------------------------
# Custom system prompt support (#1728)
# ---------------------------------------------------------------------------


class TestCustomSystemPrompt:
    """Verify the system_prompt parameter propagation (#1728)."""

    def test_extract_passes_custom_system_prompt(self) -> None:
        """extract() passes system_prompt through to the API call."""
        response = _make_response(SINGLE_RULING_JSON)
        mock_client = MagicMock()
        mock_client.messages.create.return_value = response

        with patch("framework.llm_extractor.anthropic.Anthropic", return_value=mock_client):
            extractor = LlmExtractor(api_key="test-key")
            rulings = extractor.extract(
                "some text",
                system_prompt="Custom prompt for county X",
            )

        # Verify the custom prompt was used
        call_kwargs = mock_client.messages.create.call_args
        assert call_kwargs.kwargs["system"] == "Custom prompt for county X"
        assert len(rulings) == 1

    def test_extract_uses_default_prompt_when_none(self) -> None:
        """extract() uses EXTRACTION_SYSTEM_PROMPT when no custom prompt given."""
        from framework.llm_schema import EXTRACTION_SYSTEM_PROMPT

        response = _make_response(SINGLE_RULING_JSON)
        mock_client = MagicMock()
        mock_client.messages.create.return_value = response

        with patch("framework.llm_extractor.anthropic.Anthropic", return_value=mock_client):
            extractor = LlmExtractor(api_key="test-key")
            extractor.extract("some text")

        call_kwargs = mock_client.messages.create.call_args
        assert call_kwargs.kwargs["system"] == EXTRACTION_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Google provider support (#1728)
# ---------------------------------------------------------------------------


class TestGoogleProvider:
    """Verify the Google provider path via _extract_chunk_google (#1728)."""

    def test_google_provider_delegates_to_call_llm(self) -> None:
        """Google provider path calls call_llm from ingestion.llm_providers."""
        from ingestion.llm_providers import LLMResponse

        mock_response = LLMResponse(
            text=SINGLE_RULING_JSON,
            input_tokens=100,
            output_tokens=50,
        )

        with patch("framework.llm_extractor._create_google_client"):
            extractor = LlmExtractor(provider="google", model="gemini-2.5-flash-lite")

        with patch("ingestion.llm_providers.call_llm", return_value=mock_response):
            rulings = extractor.extract("some text")

        assert len(rulings) == 1
        assert rulings[0].extracted_case_number == "2024-01393434"

    def test_google_provider_returns_empty_on_api_failure(self) -> None:
        """Google provider returns empty list when call_llm returns None."""
        with patch("framework.llm_extractor._create_google_client"):
            extractor = LlmExtractor(provider="google")

        with patch("ingestion.llm_providers.call_llm", return_value=None):
            rulings = extractor.extract("some text")

        assert rulings == []

    def test_google_provider_with_custom_system_prompt(self) -> None:
        """Google provider passes custom system_prompt to call_llm (#1728)."""
        from ingestion.llm_providers import LLMResponse

        mock_response = LLMResponse(
            text=SINGLE_RULING_JSON,
            input_tokens=100,
            output_tokens=50,
        )

        with patch("framework.llm_extractor._create_google_client"):
            extractor = LlmExtractor(provider="google", model="gemini-2.5-flash-lite")

        with patch("ingestion.llm_providers.call_llm", return_value=mock_response) as mock_call:
            extractor.extract("some text", system_prompt="Custom Riverside prompt")

        # Verify call_llm was called with the custom prompt
        call_kwargs = mock_call.call_args
        assert call_kwargs.kwargs["system_prompt"] == "Custom Riverside prompt"
        assert call_kwargs.kwargs["provider"] == "google"
        assert call_kwargs.kwargs["model"] == "gemini-2.5-flash-lite"

    def test_google_provider_passes_metadata(self) -> None:
        """Google provider passes metadata through to _parse_response (#1728)."""
        from ingestion.llm_providers import LLMResponse

        mock_response = LLMResponse(
            text=SINGLE_RULING_JSON,
            input_tokens=100,
            output_tokens=50,
        )

        with patch("framework.llm_extractor._create_google_client"):
            extractor = LlmExtractor(provider="google", model="gemini-2.5-flash-lite")

        metadata = {"judge_name": "Authoritative Judge Name"}

        with patch("ingestion.llm_providers.call_llm", return_value=mock_response):
            with patch.object(extractor, "_parse_response", wraps=extractor._parse_response) as spy:
                extractor.extract("some text", metadata=metadata)

        # Verify _parse_response was called with the metadata
        spy.assert_called_once()
        call_args = spy.call_args
        assert call_args[0][1] == metadata  # second positional arg is metadata

    def test_google_provider_tracks_token_usage(self) -> None:
        """Google provider updates token usage from LLMResponse."""
        from ingestion.llm_providers import LLMResponse

        mock_response = LLMResponse(
            text=SINGLE_RULING_JSON,
            input_tokens=500,
            output_tokens=200,
        )

        with patch("framework.llm_extractor._create_google_client"):
            extractor = LlmExtractor(provider="google")

        with patch("ingestion.llm_providers.call_llm", return_value=mock_response):
            with patch.object(extractor, "_log_usage") as mock_log:
                extractor.extract("some text")

        # Verify token usage was logged
        mock_log.assert_called_once()
        usage = mock_log.call_args[0][0]
        assert usage.input_tokens == 500
        assert usage.output_tokens == 200
        assert usage.api_calls == 1


# ---------------------------------------------------------------------------
# Integration test placeholder (skipped without API key)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    True,  # noqa: FBT003 — Always skip in automated testing
    reason="Integration test requires ANTHROPIC_API_KEY — run manually",
)
class TestIntegration:
    """Integration tests against the real Anthropic API.

    To run: set ANTHROPIC_API_KEY and change skipif to False.
    """

    def test_real_extraction(self) -> None:
        extractor = LlmExtractor()
        text = (
            "TENTATIVE RULINGS\n"
            "DEPT C25 / Judge Gassia Apkarian\n"
            "February 24, 2026\n\n"
            "Case No. 2024-01393434\n"
            "Martinez v. ABC Manufacturing Inc.\n"
            "Motion for Summary Adjudication\n"
            "The motion for summary adjudication is DENIED.\n"
        )
        rulings = extractor.extract(text)
        assert len(rulings) >= 1
        assert rulings[0].extracted_case_number is not None
