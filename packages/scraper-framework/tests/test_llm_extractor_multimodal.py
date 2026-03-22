"""Tests for multimodal extraction in LlmExtractor (#1589).

Validates:
1. LlmExtractor accepts provider parameter ("anthropic" or "google").
2. extract_from_pdf() renders pages and sends images to the configured provider.
3. Existing extract(text) path remains unchanged.
4. Error handling (empty PDF, render failures, API failures).
5. _render_pdf_pages helper.
6. _create_google_client helper.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import anthropic
import pytest

from framework.llm_extractor import (
    LlmExtractor,
    _create_google_client,
    _render_pdf_pages,
)
from framework.llm_schema import (
    ExtractionOutcome,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SAMPLE_PDF_PATH = FIXTURES_DIR / "sample_ruling.pdf"

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
                "ruling_text": "The motion for summary adjudication is DENIED.",
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
            },
            {
                "extracted_case_number": "CVPS2400892",
                "extracted_case_title": "Thompson v. City of Palm Springs",
                "case_type": "civil",
                "outcome": "denied",
                "motion_type": "demurrer",
                "ruling_text": "The demurrer is OVERRULED.",
                "hearing_date": "2026-03-02",
            },
        ],
    }
)


@pytest.fixture()
def sample_pdf_bytes() -> bytes:
    """Load the sample PDF fixture."""
    return SAMPLE_PDF_PATH.read_bytes()


def _make_llm_response(text: str) -> MagicMock:
    """Create a mock LLMResponse from llm_providers."""
    from ingestion.llm_providers import LLMResponse

    return LLMResponse(text=text, input_tokens=500, output_tokens=200)


# ---------------------------------------------------------------------------
# Provider parameter tests
# ---------------------------------------------------------------------------


class TestProviderParameter:
    """Tests for the provider parameter on LlmExtractor."""

    def test_default_provider_is_anthropic(self) -> None:
        """Without explicit provider, defaults to anthropic."""
        with patch.object(anthropic, "Anthropic"):
            ext = LlmExtractor(api_key="test-key")
        assert ext._provider == "anthropic"

    def test_anthropic_provider_creates_anthropic_client(self) -> None:
        """provider='anthropic' creates an Anthropic client."""
        with patch.object(anthropic, "Anthropic") as mock_cls:
            mock_cls.return_value = MagicMock()
            ext = LlmExtractor(provider="anthropic", api_key="test-key")
        assert ext._provider == "anthropic"
        mock_cls.assert_called_once_with(api_key="test-key")

    def test_google_provider_creates_google_client(self) -> None:
        """provider='google' creates a Google GenAI client."""
        mock_client = MagicMock()
        with patch("framework.llm_extractor._create_google_client", return_value=mock_client):
            ext = LlmExtractor(provider="google", api_key="test-key")
        assert ext._provider == "google"
        assert ext._client is mock_client

    def test_google_default_model(self) -> None:
        """Google provider defaults to gemini-2.5-flash-lite."""
        mock_client = MagicMock()
        with patch("framework.llm_extractor._create_google_client", return_value=mock_client):
            ext = LlmExtractor(provider="google", api_key="test-key")
        assert ext._model == "gemini-2.5-flash-lite"

    def test_anthropic_default_model(self) -> None:
        """Anthropic provider defaults to DEFAULT_HAIKU_MODEL."""
        from judgemind_config import DEFAULT_HAIKU_MODEL

        with patch.object(anthropic, "Anthropic"):
            ext = LlmExtractor(provider="anthropic", api_key="test-key")
        assert ext._model == DEFAULT_HAIKU_MODEL

    def test_custom_model_overrides_default(self) -> None:
        """Explicit model overrides provider default."""
        mock_client = MagicMock()
        with patch("framework.llm_extractor._create_google_client", return_value=mock_client):
            ext = LlmExtractor(
                provider="google",
                model="gemini-2.5-flash-lite-preview-06-17",
                api_key="test-key",
            )
        assert ext._model == "gemini-2.5-flash-lite-preview-06-17"

    def test_backward_compatible_no_provider(self) -> None:
        """Existing code without provider= still works (defaults to anthropic)."""
        with patch.object(anthropic, "Anthropic"):
            ext = LlmExtractor(api_key="test-key")
        assert ext._provider == "anthropic"


# ---------------------------------------------------------------------------
# extract_from_pdf tests
# ---------------------------------------------------------------------------


class TestExtractFromPdf:
    """Tests for the multimodal extract_from_pdf method."""

    def test_empty_bytes_returns_empty(self) -> None:
        """Empty PDF bytes should return empty list."""
        with patch.object(anthropic, "Anthropic"):
            ext = LlmExtractor(api_key="test-key")
        assert ext.extract_from_pdf(b"") == []

    def test_renders_pages_and_calls_llm(self, sample_pdf_bytes: bytes) -> None:
        """extract_from_pdf renders pages to images and sends them to the LLM."""
        with patch.object(anthropic, "Anthropic"):
            ext = LlmExtractor(api_key="test-key")

        mock_response = _make_llm_response(SINGLE_RULING_JSON)
        with (
            patch(
                "framework.llm_extractor._render_pdf_pages",
                return_value=[(b"\x89PNG_fake", "image/png")],
            ) as mock_render,
            patch(
                "ingestion.llm_providers.call_llm_with_images",
                return_value=mock_response,
            ) as mock_call,
        ):
            rulings = ext.extract_from_pdf(sample_pdf_bytes)

        mock_render.assert_called_once_with(sample_pdf_bytes, 20)
        mock_call.assert_called_once()
        assert len(rulings) == 1
        assert rulings[0].extracted_case_number == "2024-01393434"
        assert rulings[0].ruling_text == "The motion for summary adjudication is DENIED."

    def test_images_passed_to_llm_call(self, sample_pdf_bytes: bytes) -> None:
        """Page images are passed as the images argument to call_llm_with_images."""
        with patch.object(anthropic, "Anthropic"):
            ext = LlmExtractor(api_key="test-key")

        fake_images = [
            (b"\x89PNG_page1", "image/png"),
            (b"\x89PNG_page2", "image/png"),
        ]
        mock_response = _make_llm_response(MULTI_RULING_JSON)
        with (
            patch(
                "framework.llm_extractor._render_pdf_pages",
                return_value=fake_images,
            ),
            patch(
                "ingestion.llm_providers.call_llm_with_images",
                return_value=mock_response,
            ) as mock_call,
        ):
            rulings = ext.extract_from_pdf(sample_pdf_bytes)

        call_kwargs = mock_call.call_args
        assert call_kwargs.kwargs["images"] == fake_images
        assert len(rulings) == 2

    def test_multi_ruling_pdf(self, sample_pdf_bytes: bytes) -> None:
        """extract_from_pdf handles multiple rulings in one PDF."""
        with patch.object(anthropic, "Anthropic"):
            ext = LlmExtractor(api_key="test-key")

        mock_response = _make_llm_response(MULTI_RULING_JSON)
        with (
            patch(
                "framework.llm_extractor._render_pdf_pages",
                return_value=[(b"\x89PNG_fake", "image/png")],
            ),
            patch(
                "ingestion.llm_providers.call_llm_with_images",
                return_value=mock_response,
            ),
        ):
            rulings = ext.extract_from_pdf(sample_pdf_bytes)

        assert len(rulings) == 2
        assert rulings[0].extracted_case_number == "CVPS2306157"
        assert rulings[0].outcome == ExtractionOutcome.GRANTED
        assert rulings[1].extracted_case_number == "CVPS2400892"
        assert rulings[1].outcome == ExtractionOutcome.DENIED

    def test_render_failure_returns_empty(self, sample_pdf_bytes: bytes) -> None:
        """If page rendering returns no pages, returns empty list."""
        with patch.object(anthropic, "Anthropic"):
            ext = LlmExtractor(api_key="test-key")

        with patch(
            "framework.llm_extractor._render_pdf_pages",
            return_value=[],
        ):
            rulings = ext.extract_from_pdf(sample_pdf_bytes)

        assert rulings == []

    def test_llm_failure_returns_empty(self, sample_pdf_bytes: bytes) -> None:
        """If the LLM API call fails, returns empty list."""
        with patch.object(anthropic, "Anthropic"):
            ext = LlmExtractor(api_key="test-key")
        ext._base_delay = 0.01
        ext._max_retries = 1

        with (
            patch(
                "framework.llm_extractor._render_pdf_pages",
                return_value=[(b"\x89PNG_fake", "image/png")],
            ),
            patch(
                "ingestion.llm_providers.call_llm_with_images",
                return_value=None,
            ),
        ):
            rulings = ext.extract_from_pdf(sample_pdf_bytes)

        assert rulings == []

    def test_metadata_passed_through(self, sample_pdf_bytes: bytes) -> None:
        """Metadata is included in the text message to the LLM."""
        with patch.object(anthropic, "Anthropic"):
            ext = LlmExtractor(api_key="test-key")

        mock_response = _make_llm_response(SINGLE_RULING_JSON)
        with (
            patch(
                "framework.llm_extractor._render_pdf_pages",
                return_value=[(b"\x89PNG_fake", "image/png")],
            ),
            patch(
                "ingestion.llm_providers.call_llm_with_images",
                return_value=mock_response,
            ) as mock_call,
        ):
            ext.extract_from_pdf(
                sample_pdf_bytes,
                metadata={"judge_name": "Override Judge", "department": "D99"},
            )

        call_kwargs = mock_call.call_args.kwargs
        text_message = call_kwargs["text_message"]
        assert "Override Judge" in text_message
        assert "D99" in text_message

    def test_max_pages_passed_to_renderer(self, sample_pdf_bytes: bytes) -> None:
        """max_pages is forwarded to _render_pdf_pages."""
        with patch.object(anthropic, "Anthropic"):
            ext = LlmExtractor(api_key="test-key")

        mock_response = _make_llm_response(SINGLE_RULING_JSON)
        with (
            patch(
                "framework.llm_extractor._render_pdf_pages",
                return_value=[(b"\x89PNG_fake", "image/png")],
            ) as mock_render,
            patch(
                "ingestion.llm_providers.call_llm_with_images",
                return_value=mock_response,
            ),
        ):
            ext.extract_from_pdf(sample_pdf_bytes, max_pages=5)

        mock_render.assert_called_once_with(sample_pdf_bytes, 5)

    def test_provider_forwarded_to_llm_call(self, sample_pdf_bytes: bytes) -> None:
        """The configured provider is forwarded to call_llm_with_images."""
        mock_client = MagicMock()
        with patch("framework.llm_extractor._create_google_client", return_value=mock_client):
            ext = LlmExtractor(provider="google", api_key="test-key")

        mock_response = _make_llm_response(SINGLE_RULING_JSON)
        with (
            patch(
                "framework.llm_extractor._render_pdf_pages",
                return_value=[(b"\x89PNG_fake", "image/png")],
            ),
            patch(
                "ingestion.llm_providers.call_llm_with_images",
                return_value=mock_response,
            ) as mock_call,
        ):
            ext.extract_from_pdf(sample_pdf_bytes)

        call_kwargs = mock_call.call_args.kwargs
        assert call_kwargs["provider"] == "google"
        assert call_kwargs["model"] == "gemini-2.5-flash-lite"

    def test_ruling_text_in_results(self, sample_pdf_bytes: bytes) -> None:
        """ruling_text field is populated in extraction results."""
        with patch.object(anthropic, "Anthropic"):
            ext = LlmExtractor(api_key="test-key")

        mock_response = _make_llm_response(SINGLE_RULING_JSON)
        with (
            patch(
                "framework.llm_extractor._render_pdf_pages",
                return_value=[(b"\x89PNG_fake", "image/png")],
            ),
            patch(
                "ingestion.llm_providers.call_llm_with_images",
                return_value=mock_response,
            ),
        ):
            rulings = ext.extract_from_pdf(sample_pdf_bytes)

        assert len(rulings) == 1
        assert rulings[0].ruling_text == "The motion for summary adjudication is DENIED."


# ---------------------------------------------------------------------------
# Existing extract(text) path — backward compatibility
# ---------------------------------------------------------------------------


class TestExtractTextPathUnchanged:
    """Verify the existing extract(text) path still works identically."""

    def test_text_extraction_still_works(self) -> None:
        """extract(text) continues to work for text-based extraction."""
        mock_client = MagicMock(spec=anthropic.Anthropic)
        mock_client.messages = MagicMock()
        with patch.object(anthropic, "Anthropic", return_value=mock_client):
            ext = LlmExtractor(api_key="test-key")
        ext._client = mock_client

        content_block = MagicMock()
        content_block.text = SINGLE_RULING_JSON
        usage = MagicMock()
        usage.input_tokens = 100
        usage.output_tokens = 50
        response = MagicMock()
        response.content = [content_block]
        response.usage = usage
        mock_client.messages.create.return_value = response

        rulings = ext.extract("Case No. 2024-01393434\nMartinez v. ABC Manufacturing")

        assert len(rulings) == 1
        assert rulings[0].extracted_case_number == "2024-01393434"
        mock_client.messages.create.assert_called_once()

    def test_empty_text_returns_empty(self) -> None:
        """extract('') still returns empty list."""
        with patch.object(anthropic, "Anthropic"):
            ext = LlmExtractor(api_key="test-key")
        assert ext.extract("") == []
        assert ext.extract("   ") == []


# ---------------------------------------------------------------------------
# _render_pdf_pages tests
# ---------------------------------------------------------------------------


class TestRenderPdfPages:
    """Tests for the _render_pdf_pages helper in llm_extractor."""

    def test_renders_pages(self, sample_pdf_bytes: bytes) -> None:
        """Renders PDF pages to PNG images."""
        pages = _render_pdf_pages(sample_pdf_bytes, max_pages=10)
        assert len(pages) >= 1
        png_bytes, media_type = pages[0]
        assert media_type == "image/png"
        assert png_bytes[:4] == b"\x89PNG"
        assert len(png_bytes) > 100

    def test_respects_max_pages(self, sample_pdf_bytes: bytes) -> None:
        """max_pages=0 should return no pages."""
        pages = _render_pdf_pages(sample_pdf_bytes, max_pages=0)
        assert len(pages) == 0

    def test_raises_for_invalid_pdf(self) -> None:
        """Invalid PDF input raises an exception."""
        with pytest.raises(Exception):
            _render_pdf_pages(b"not a pdf", max_pages=10)


# ---------------------------------------------------------------------------
# _create_google_client tests
# ---------------------------------------------------------------------------


class TestCreateGoogleClient:
    """Tests for the _create_google_client helper."""

    def test_with_explicit_key(self) -> None:
        """Creates client with explicit api_key."""
        mock_client = MagicMock()
        with patch("google.genai.Client", return_value=mock_client) as mock_cls:
            result = _create_google_client(api_key="test-key")
        assert result is mock_client
        mock_cls.assert_called_once_with(api_key="test-key")

    def test_with_env_key(self) -> None:
        """Falls back to GOOGLE_API_KEY env var."""
        mock_client = MagicMock()
        with (
            patch.dict("os.environ", {"GOOGLE_API_KEY": "env-key"}),
            patch("google.genai.Client", return_value=mock_client) as mock_cls,
        ):
            result = _create_google_client()
        assert result is mock_client
        mock_cls.assert_called_once_with(api_key="env-key")

    def test_raises_without_key(self) -> None:
        """Raises ValueError when no API key is available."""
        with (
            patch.dict("os.environ", {}, clear=True),
            pytest.raises(ValueError, match="No Google API key"),
        ):
            _create_google_client()


# ---------------------------------------------------------------------------
# Token usage tracking for multimodal path
# ---------------------------------------------------------------------------


class TestMultimodalTokenUsage:
    """Tests for token usage tracking in the multimodal path."""

    def test_token_usage_logged(self, sample_pdf_bytes: bytes) -> None:
        """Token usage is logged after multimodal extraction."""
        with patch.object(anthropic, "Anthropic"):
            ext = LlmExtractor(api_key="test-key")

        mock_response = _make_llm_response(SINGLE_RULING_JSON)
        with (
            patch(
                "framework.llm_extractor._render_pdf_pages",
                return_value=[(b"\x89PNG_fake", "image/png")],
            ),
            patch(
                "ingestion.llm_providers.call_llm_with_images",
                return_value=mock_response,
            ),
            patch("framework.llm_extractor.logger") as mock_logger,
        ):
            ext.extract_from_pdf(sample_pdf_bytes)

        # Verify token_usage was logged.
        usage_calls = [
            c
            for c in mock_logger.info.call_args_list
            if c.args and c.args[0] == "llm_extractor.token_usage"
        ]
        assert len(usage_calls) == 1
        assert usage_calls[0].kwargs["input_tokens"] == 500
        assert usage_calls[0].kwargs["output_tokens"] == 200
        assert usage_calls[0].kwargs["api_calls"] == 1


# ---------------------------------------------------------------------------
# Retry and error handling in _extract_images
# ---------------------------------------------------------------------------


class TestExtractImagesRetry:
    """Tests for retry and error handling in the multimodal image extraction path."""

    def test_retries_on_none_response(self, sample_pdf_bytes: bytes) -> None:
        """When LLM returns None, retries before giving up."""
        with patch.object(anthropic, "Anthropic"):
            ext = LlmExtractor(api_key="test-key")
        ext._base_delay = 0.01
        ext._max_retries = 3

        mock_response = _make_llm_response(SINGLE_RULING_JSON)
        # First two calls return None, third succeeds.
        with (
            patch(
                "framework.llm_extractor._render_pdf_pages",
                return_value=[(b"\x89PNG_fake", "image/png")],
            ),
            patch(
                "ingestion.llm_providers.call_llm_with_images",
                side_effect=[None, None, mock_response],
            ) as mock_call,
        ):
            rulings = ext.extract_from_pdf(sample_pdf_bytes)

        assert len(rulings) == 1
        assert mock_call.call_count == 3

    def test_exhausts_retries_on_none_response(self, sample_pdf_bytes: bytes) -> None:
        """When all retries return None, returns empty list."""
        with patch.object(anthropic, "Anthropic"):
            ext = LlmExtractor(api_key="test-key")
        ext._base_delay = 0.01
        ext._max_retries = 2

        with (
            patch(
                "framework.llm_extractor._render_pdf_pages",
                return_value=[(b"\x89PNG_fake", "image/png")],
            ),
            patch(
                "ingestion.llm_providers.call_llm_with_images",
                return_value=None,
            ),
        ):
            rulings = ext.extract_from_pdf(sample_pdf_bytes)

        assert rulings == []

    def test_retries_on_exception(self, sample_pdf_bytes: bytes) -> None:
        """When LLM call raises an exception, retries before giving up."""
        with patch.object(anthropic, "Anthropic"):
            ext = LlmExtractor(api_key="test-key")
        ext._base_delay = 0.01
        ext._max_retries = 3

        mock_response = _make_llm_response(SINGLE_RULING_JSON)
        with (
            patch(
                "framework.llm_extractor._render_pdf_pages",
                return_value=[(b"\x89PNG_fake", "image/png")],
            ),
            patch(
                "ingestion.llm_providers.call_llm_with_images",
                side_effect=[RuntimeError("network"), RuntimeError("timeout"), mock_response],
            ) as mock_call,
        ):
            rulings = ext.extract_from_pdf(sample_pdf_bytes)

        assert len(rulings) == 1
        assert mock_call.call_count == 3

    def test_exhausts_retries_on_exception(self, sample_pdf_bytes: bytes) -> None:
        """When all retries raise exceptions, returns empty list."""
        with patch.object(anthropic, "Anthropic"):
            ext = LlmExtractor(api_key="test-key")
        ext._base_delay = 0.01
        ext._max_retries = 2

        with (
            patch(
                "framework.llm_extractor._render_pdf_pages",
                return_value=[(b"\x89PNG_fake", "image/png")],
            ),
            patch(
                "ingestion.llm_providers.call_llm_with_images",
                side_effect=RuntimeError("persistent failure"),
            ),
        ):
            rulings = ext.extract_from_pdf(sample_pdf_bytes)

        assert rulings == []


# ---------------------------------------------------------------------------
# _build_user_message_for_images
# ---------------------------------------------------------------------------


class TestBuildUserMessageForImages:
    """Tests for the image extraction text message builder."""

    def test_no_metadata(self) -> None:
        """Without metadata, produces a generic extraction message."""
        msg = LlmExtractor._build_user_message_for_images(None)
        assert "Extract ALL structured fields" in msg

    def test_with_all_metadata(self) -> None:
        """With all metadata keys, includes them in the message."""
        msg = LlmExtractor._build_user_message_for_images(
            {
                "judge_name": "Test Judge",
                "department": "D99",
                "hearing_date": "2026-03-01",
            }
        )
        assert "Test Judge" in msg
        assert "D99" in msg
        assert "2026-03-01" in msg
        assert "Extract ALL structured fields" in msg

    def test_with_hearing_date_only(self) -> None:
        """With only hearing_date, includes it in the message."""
        msg = LlmExtractor._build_user_message_for_images({"hearing_date": "2026-04-15"})
        assert "2026-04-15" in msg


# ---------------------------------------------------------------------------
# ExtractedRuling ruling_text field
# ---------------------------------------------------------------------------


class TestExtractedRulingRulingText:
    """Verify ExtractedRuling includes ruling_text field."""

    def test_ruling_text_field_exists(self) -> None:
        """ExtractedRuling has a ruling_text field."""
        from framework.llm_schema import ExtractedRuling

        ruling = ExtractedRuling(ruling_text="The motion is GRANTED.")
        assert ruling.ruling_text == "The motion is GRANTED."

    def test_ruling_text_defaults_to_none(self) -> None:
        """ruling_text defaults to None when not provided."""
        from framework.llm_schema import ExtractedRuling

        ruling = ExtractedRuling()
        assert ruling.ruling_text is None


# ---------------------------------------------------------------------------
# System prompt includes ruling_text
# ---------------------------------------------------------------------------


class TestSystemPromptRulingText:
    """Verify the system prompt requests ruling_text per case."""

    def test_ruling_text_in_prompt(self) -> None:
        """The extraction system prompt includes ruling_text in the output format."""
        from framework.llm_schema import EXTRACTION_SYSTEM_PROMPT

        assert "ruling_text" in EXTRACTION_SYSTEM_PROMPT

    def test_ruling_text_in_output_example(self) -> None:
        """The output format example includes ruling_text."""
        from framework.llm_schema import EXTRACTION_SYSTEM_PROMPT

        # Check both the rule about ruling text and the output format
        assert "ruling_text" in EXTRACTION_SYSTEM_PROMPT
        assert "The motion for summary judgment is GRANTED..." in EXTRACTION_SYSTEM_PROMPT
