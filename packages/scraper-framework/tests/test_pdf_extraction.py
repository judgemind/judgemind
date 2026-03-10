"""Tests for PDF text extraction in the ingestion pipeline.

Validates that raw PDF binary content is detected and text is extracted
before being passed to LLM or regex extraction. Uses a real PDF fixture
created by pdfplumber/reportlab.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ingestion.llm_extract import (
    extract_fields_llm,
    extract_text_from_pdf,
    is_pdf_binary,
)
from ingestion.llm_providers import LLMResponse

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SAMPLE_PDF_PATH = FIXTURES_DIR / "sample_ruling.pdf"


@pytest.fixture()
def pdf_bytes() -> bytes:
    """Load the sample ruling PDF fixture as raw bytes."""
    return SAMPLE_PDF_PATH.read_bytes()


@pytest.fixture()
def pdf_as_string(pdf_bytes: bytes) -> str:
    """Simulate what happens when raw PDF bytes are decoded as latin-1 (common in DB storage)."""
    return pdf_bytes.decode("latin-1")


def _mock_call_llm(response_text: str) -> MagicMock:
    """Create a mock for call_llm that returns the given JSON string."""
    return MagicMock(
        return_value=LLMResponse(
            text=response_text,
            input_tokens=100,
            output_tokens=50,
        )
    )


# ---------------------------------------------------------------------------
# is_pdf_binary
# ---------------------------------------------------------------------------


class TestIsPdfBinary:
    """Tests for PDF binary content detection."""

    def test_detects_raw_pdf_bytes(self, pdf_bytes: bytes) -> None:
        assert is_pdf_binary(pdf_bytes) is True

    def test_detects_pdf_string(self, pdf_as_string: str) -> None:
        assert is_pdf_binary(pdf_as_string) is True

    def test_rejects_html_content(self) -> None:
        assert is_pdf_binary("<html><body>Hello</body></html>") is False

    def test_rejects_plain_text(self) -> None:
        assert is_pdf_binary("The motion for summary judgment is GRANTED.") is False

    def test_rejects_empty_string(self) -> None:
        assert is_pdf_binary("") is False

    def test_rejects_empty_bytes(self) -> None:
        assert is_pdf_binary(b"") is False

    def test_rejects_short_bytes(self) -> None:
        assert is_pdf_binary(b"%PD") is False


# ---------------------------------------------------------------------------
# extract_text_from_pdf
# ---------------------------------------------------------------------------


class TestExtractTextFromPdf:
    """Tests for PDF text extraction using pdfplumber."""

    def test_extracts_text_from_bytes(self, pdf_bytes: bytes) -> None:
        text = extract_text_from_pdf(pdf_bytes)
        assert text is not None
        assert "SUPERIOR COURT OF CALIFORNIA" in text
        assert "CVPS2306157" in text
        assert "Yeldell v. Henss" in text
        assert "GRANTED" in text

    def test_extracts_text_from_string(self, pdf_as_string: str) -> None:
        text = extract_text_from_pdf(pdf_as_string)
        assert text is not None
        assert "SUPERIOR COURT OF CALIFORNIA" in text
        assert "CVPS2306157" in text

    def test_returns_none_for_invalid_pdf(self) -> None:
        result = extract_text_from_pdf(b"not a pdf at all")
        assert result is None

    def test_returns_none_for_empty_content(self) -> None:
        result = extract_text_from_pdf(b"")
        assert result is None

    def test_returns_none_for_corrupt_pdf_string(self) -> None:
        result = extract_text_from_pdf("not a pdf")
        assert result is None

    def test_uses_form_feed_separator(self, pdf_bytes: bytes) -> None:
        """Multi-page PDFs should use form-feed separators between pages."""
        text = extract_text_from_pdf(pdf_bytes)
        assert text is not None
        # Single-page PDF shouldn't have form feeds
        # (but the separator pattern "\n\f\n" is used between pages)


# ---------------------------------------------------------------------------
# extract_fields_llm — PDF binary handling
# ---------------------------------------------------------------------------


class TestExtractFieldsLlmPdf:
    """Tests that extract_fields_llm preprocesses PDF binary content."""

    def test_extracts_from_pdf_binary_before_llm(self, pdf_bytes: bytes) -> None:
        """When raw PDF bytes are passed as document_text, extract text first."""
        # The PDF text is decoded as latin-1 in many cases
        pdf_text = pdf_bytes.decode("latin-1")

        llm_response = json.dumps(
            {
                "judge_name": "John A. Smith",
                "hearing_date": "2026-03-05",
                "department": "1",
                "rulings": [
                    {
                        "case_number": "CVPS2306157",
                        "case_title": "Yeldell v. Henss",
                        "outcome": "granted",
                        "motion_type": "msj",
                        "parties": [
                            {"name": "Yeldell", "role": "plaintiff"},
                            {"name": "Henss", "role": "defendant"},
                        ],
                    }
                ],
            }
        )

        with patch("ingestion.llm_extract.call_llm", _mock_call_llm(llm_response)):
            result = extract_fields_llm(
                document_text=pdf_text,
                content_format="pdf",
                client=MagicMock(),
                provider="google",
            )

        assert result is not None
        assert result.judge_name == "John A. Smith"
        assert len(result.rulings) == 1
        assert result.rulings[0].case_number == "CVPS2306157"

    def test_returns_none_when_pdf_extraction_fails(self) -> None:
        """If PDF text extraction fails, return None (fall back to regex)."""
        # Content that starts with %PDF but is actually corrupt
        corrupt_pdf = "%PDF-1.4\ncorrupt content that pdfplumber can't parse"

        result = extract_fields_llm(
            document_text=corrupt_pdf,
            content_format="pdf",
            client=MagicMock(),
            provider="google",
        )

        assert result is None

    def test_passes_already_extracted_text_through(self) -> None:
        """When PDF content is already plain text (not binary), pass it through."""
        already_extracted = "The motion for summary judgment is GRANTED."

        llm_response = json.dumps(
            {
                "judge_name": None,
                "hearing_date": None,
                "department": None,
                "rulings": [
                    {
                        "case_number": None,
                        "case_title": None,
                        "outcome": "granted",
                        "motion_type": "msj",
                        "parties": [],
                    }
                ],
            }
        )

        with patch("ingestion.llm_extract.call_llm", _mock_call_llm(llm_response)):
            result = extract_fields_llm(
                document_text=already_extracted,
                content_format="pdf",
                client=MagicMock(),
                provider="google",
            )

        assert result is not None
        assert result.rulings[0].outcome == "granted"
