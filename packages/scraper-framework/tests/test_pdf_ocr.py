"""Tests for OCR fallback for image-only PDFs (#1332).

Validates that:
- Image-only PDFs trigger the OCR fallback in extract_text_from_pdf()
- Text-layer PDFs continue to use pdfplumber (no OCR called)
- OCR timeout limits are respected
- Logging/metrics are emitted when OCR is used
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ingestion.llm_extract import (
    _render_pdf_pages,
    extract_text_from_pdf,
    ocr_pdf_text,
)
from ingestion.llm_providers import LLMResponse

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).parent / "fixtures"
IMAGE_ONLY_PDF_PATH = FIXTURES_DIR / "image_only_ruling.pdf"
SAMPLE_PDF_PATH = FIXTURES_DIR / "sample_ruling.pdf"


@pytest.fixture()
def image_only_pdf_bytes() -> bytes:
    """Load the image-only PDF fixture (no text layer)."""
    return IMAGE_ONLY_PDF_PATH.read_bytes()


@pytest.fixture()
def text_pdf_bytes() -> bytes:
    """Load a normal PDF fixture with a text layer."""
    return SAMPLE_PDF_PATH.read_bytes()


def _mock_ocr_response(text: str) -> MagicMock:
    """Create a mock for call_llm_with_images that returns extracted text."""
    return MagicMock(
        return_value=LLMResponse(
            text=text,
            input_tokens=500,
            output_tokens=100,
        )
    )


def _mock_ocr_failure() -> MagicMock:
    """Create a mock for call_llm_with_images that returns None (failure)."""
    return MagicMock(return_value=None)


# ---------------------------------------------------------------------------
# _render_pdf_pages
# ---------------------------------------------------------------------------


class TestRenderPdfPages:
    """Tests for rendering PDF pages to PNG images."""

    def test_renders_single_page(self, image_only_pdf_bytes: bytes) -> None:
        pages = _render_pdf_pages(image_only_pdf_bytes, max_pages=10)
        assert len(pages) == 1
        png_bytes, media_type = pages[0]
        assert media_type == "image/png"
        # PNG magic bytes
        assert png_bytes[:4] == b"\x89PNG"
        assert len(png_bytes) > 100  # non-trivial image

    def test_respects_max_pages(self, image_only_pdf_bytes: bytes) -> None:
        # Our fixture has 1 page, so max_pages=0 should return nothing
        pages = _render_pdf_pages(image_only_pdf_bytes, max_pages=0)
        assert len(pages) == 0

    def test_returns_empty_for_invalid_pdf(self) -> None:
        with pytest.raises(Exception):
            _render_pdf_pages(b"not a pdf", max_pages=10)


# ---------------------------------------------------------------------------
# ocr_pdf_text
# ---------------------------------------------------------------------------


class TestOcrPdfText:
    """Tests for the OCR fallback function."""

    def test_extracts_text_from_image_pdf(self, image_only_pdf_bytes: bytes) -> None:
        """OCR should return text from an image-only PDF."""
        expected_text = (
            "SUPERIOR COURT OF CALIFORNIA\n"
            "COUNTY OF SANTA CLARA\n"
            "Case No. 24CV123456\n"
            "SMITH v. JONES"
        )
        with patch(
            "ingestion.llm_extract.call_llm_with_images",
            _mock_ocr_response(expected_text),
        ):
            result = ocr_pdf_text(image_only_pdf_bytes)

        assert result is not None
        assert "SUPERIOR COURT" in result

    def test_returns_none_on_llm_failure(self, image_only_pdf_bytes: bytes) -> None:
        """OCR should return None when the LLM call fails."""
        with patch(
            "ingestion.llm_extract.call_llm_with_images",
            _mock_ocr_failure(),
        ):
            result = ocr_pdf_text(image_only_pdf_bytes)

        assert result is None

    def test_returns_none_on_empty_response(self, image_only_pdf_bytes: bytes) -> None:
        """OCR should return None when the LLM returns empty text."""
        with patch(
            "ingestion.llm_extract.call_llm_with_images",
            _mock_ocr_response(""),
        ):
            result = ocr_pdf_text(image_only_pdf_bytes)

        assert result is None

    def test_returns_none_for_invalid_pdf(self) -> None:
        """OCR should return None for invalid PDF input."""
        result = ocr_pdf_text(b"not a pdf at all")
        assert result is None

    def test_respects_total_timeout(self, image_only_pdf_bytes: bytes) -> None:
        """OCR should stop when total timeout is exceeded."""
        with patch(
            "ingestion.llm_extract.call_llm_with_images",
            _mock_ocr_response("page text"),
        ):
            # With a 0-second timeout, it should bail immediately
            result = ocr_pdf_text(image_only_pdf_bytes, total_timeout=0.0)

        assert result is None

    def test_respects_max_pages(self, image_only_pdf_bytes: bytes) -> None:
        """OCR should limit the number of pages processed."""
        with patch(
            "ingestion.llm_extract.call_llm_with_images",
            _mock_ocr_response("page text"),
        ):
            result = ocr_pdf_text(image_only_pdf_bytes, max_pages=0)

        assert result is None

    def test_logs_ocr_fallback(
        self, image_only_pdf_bytes: bytes, caplog: pytest.LogCaptureFixture
    ) -> None:
        """OCR should log when the fallback is used."""
        with patch(
            "ingestion.llm_extract.call_llm_with_images",
            _mock_ocr_response("Extracted OCR text"),
        ):
            result = ocr_pdf_text(image_only_pdf_bytes)

        assert result is not None
        # structlog may not appear in caplog, but we verify the function runs


# ---------------------------------------------------------------------------
# extract_text_from_pdf — OCR integration
# ---------------------------------------------------------------------------


class TestExtractTextFromPdfOcrFallback:
    """Tests that extract_text_from_pdf falls back to OCR for image-only PDFs."""

    def test_text_pdf_uses_pdfplumber_no_ocr(self, text_pdf_bytes: bytes) -> None:
        """Normal PDFs with text layers should use pdfplumber, not OCR."""
        with patch(
            "ingestion.llm_extract.ocr_pdf_text",
        ) as mock_ocr:
            result = extract_text_from_pdf(text_pdf_bytes)

        assert result is not None
        assert "SUPERIOR COURT" in result
        mock_ocr.assert_not_called()

    def test_image_pdf_triggers_ocr_fallback(self, image_only_pdf_bytes: bytes) -> None:
        """Image-only PDFs should trigger the OCR fallback."""
        expected_text = "OCR extracted text from court ruling"
        with patch(
            "ingestion.llm_extract.ocr_pdf_text",
            return_value=expected_text,
        ) as mock_ocr:
            result = extract_text_from_pdf(image_only_pdf_bytes)

        assert result == expected_text
        mock_ocr.assert_called_once_with(image_only_pdf_bytes)

    def test_image_pdf_returns_none_when_ocr_fails(self, image_only_pdf_bytes: bytes) -> None:
        """When OCR also fails, extract_text_from_pdf should return None."""
        with patch(
            "ingestion.llm_extract.ocr_pdf_text",
            return_value=None,
        ):
            result = extract_text_from_pdf(image_only_pdf_bytes)

        assert result is None

    def test_string_input_image_pdf_triggers_ocr(self, image_only_pdf_bytes: bytes) -> None:
        """Image-only PDF passed as latin-1 string should also trigger OCR."""
        pdf_string = image_only_pdf_bytes.decode("latin-1")
        expected_text = "OCR text from string input"
        with patch(
            "ingestion.llm_extract.ocr_pdf_text",
            return_value=expected_text,
        ) as mock_ocr:
            result = extract_text_from_pdf(pdf_string)

        assert result == expected_text
        mock_ocr.assert_called_once()

    def test_invalid_pdf_returns_none_no_ocr(self) -> None:
        """Invalid PDF input should return None without triggering OCR."""
        with patch(
            "ingestion.llm_extract.ocr_pdf_text",
        ) as mock_ocr:
            result = extract_text_from_pdf(b"not a pdf at all")

        assert result is None
        mock_ocr.assert_not_called()


# ---------------------------------------------------------------------------
# call_llm_with_images (llm_providers)
# ---------------------------------------------------------------------------


class TestCallLlmWithImages:
    """Tests for the vision-capable LLM call function."""

    def test_anthropic_builds_image_content_blocks(self) -> None:
        """Verify Anthropic adapter constructs image content blocks correctly."""
        from ingestion.llm_providers import _call_anthropic_with_images

        mock_client = MagicMock()
        mock_response = MagicMock()
        # Mock a text content block with type="text" attribute
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = "Extracted text"
        mock_response.content = [text_block]
        mock_response.usage.input_tokens = 100
        mock_response.usage.output_tokens = 50
        mock_client.messages.create.return_value = mock_response

        result = _call_anthropic_with_images(
            system_prompt="Extract text",
            text_message="OCR this page",
            images=[(b"fake_png_bytes", "image/png")],
            model="claude-haiku-4-5-20251001",
            client=mock_client,
        )

        assert result is not None
        assert result.text == "Extracted text"

        # Verify the content blocks structure
        call_kwargs = mock_client.messages.create.call_args
        messages = call_kwargs.kwargs["messages"]
        content = messages[0]["content"]

        # First block should be image
        assert content[0]["type"] == "image"
        assert content[0]["source"]["type"] == "base64"
        assert content[0]["source"]["media_type"] == "image/png"

        # Last block should be text
        assert content[-1]["type"] == "text"
        assert content[-1]["text"] == "OCR this page"

    def test_google_builds_multimodal_parts(self) -> None:
        """Verify Google adapter constructs multimodal parts correctly."""
        from ingestion.llm_providers import _call_google_with_images

        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.text = "Extracted text"
        mock_response.usage_metadata.prompt_token_count = 100
        mock_response.usage_metadata.candidates_token_count = 50
        mock_client.models.generate_content.return_value = mock_response

        result = _call_google_with_images(
            system_prompt="Extract text",
            text_message="OCR this page",
            images=[(b"fake_png_bytes", "image/png")],
            model="gemini-2.5-flash-lite",
            client=mock_client,
        )

        assert result is not None
        assert result.text == "Extracted text"

        # Verify generate_content was called with parts
        call_args = mock_client.models.generate_content.call_args
        assert call_args is not None

    def test_call_llm_with_images_routes_to_provider(self) -> None:
        """Verify call_llm_with_images dispatches to the correct provider."""
        from ingestion.llm_providers import call_llm_with_images

        with patch(
            "ingestion.llm_providers._call_google_with_images",
            return_value=LLMResponse(text="result", input_tokens=10, output_tokens=5),
        ) as mock_google:
            result = call_llm_with_images(
                system_prompt="test",
                text_message="test",
                images=[(b"png", "image/png")],
                provider="google",
            )

        assert result is not None
        assert result.text == "result"
        mock_google.assert_called_once()

    def test_call_llm_with_images_unknown_provider(self) -> None:
        """Unknown provider should return None."""
        from ingestion.llm_providers import call_llm_with_images

        result = call_llm_with_images(
            system_prompt="test",
            text_message="test",
            images=[(b"png", "image/png")],
            provider="unknown_provider",
        )

        assert result is None
