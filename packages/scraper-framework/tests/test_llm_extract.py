"""Tests for LLM-based field extraction (llm_extract.py).

All tests mock the Anthropic API — no real API calls are made.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import anthropic
import pytest

from ingestion.llm_extract import (
    _normalize_case_number,
    _parse_response,
    extract_fields_llm,
    preprocess_html,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _make_api_response(text: str) -> MagicMock:
    """Create a mock Anthropic API response with the given text content."""
    block = MagicMock()
    block.text = text
    response = MagicMock()
    response.content = [block]
    return response


def _mock_client(response_text: str) -> MagicMock:
    """Create a mock Anthropic client that returns the given JSON string."""
    client = MagicMock(spec=anthropic.Anthropic)
    client.messages.create.return_value = _make_api_response(response_text)
    return client


# ---------------------------------------------------------------------------
# preprocess_html
# ---------------------------------------------------------------------------


class TestPreprocessHtml:
    """Tests for HTML preprocessing / boilerplate stripping."""

    def test_extracts_speech_synthesis_div(self) -> None:
        html_content = """
        <html><body>
        <div id="headerWrap">HEADER STUFF</div>
        <td><div id="speechSynthesis" class="Print">
            <b>DEPARTMENT 3</b> Case Number: 24NNCV02551
            <p>Smith v. Jones</p>
            <p>Motion for summary judgment is GRANTED.</p>
        </div></td>
        </body></html>
        """
        result = preprocess_html(html_content)
        assert "DEPARTMENT 3" in result
        assert "24NNCV02551" in result
        assert "GRANTED" in result
        # Boilerplate should be stripped
        assert "HEADER STUFF" not in result

    def test_strips_style_and_script_tags(self) -> None:
        html_content = """
        <html><body>
        <td><div id="speechSynthesis">
            <style>.foo { color: red; }</style>
            <script>alert('hi');</script>
            <p>Real content here</p>
        </div></td>
        </body></html>
        """
        result = preprocess_html(html_content)
        assert "color: red" not in result
        assert "alert" not in result
        assert "Real content here" in result

    def test_decodes_html_entities(self) -> None:
        html_content = """
        <html><body>
        <td><div id="speechSynthesis">
            Smith &amp; Jones &mdash; case #1
        </div></td>
        </body></html>
        """
        result = preprocess_html(html_content)
        assert "Smith & Jones" in result

    def test_fallback_to_body_when_no_speech_synthesis(self) -> None:
        html_content = """
        <html><body>
        <div class="ruling">Motion DENIED.</div>
        </body></html>
        """
        result = preprocess_html(html_content)
        assert "Motion DENIED" in result

    def test_collapses_whitespace(self) -> None:
        html_content = """
        <html><body>
        <td><div id="speechSynthesis">
            word1     word2
        </div></td>
        </body></html>
        """
        result = preprocess_html(html_content)
        assert "word1 word2" in result

    def test_real_la_fixture(self) -> None:
        """Test preprocessing against a real LA court ruling fixture."""
        fixture_path = FIXTURES_DIR / "la_ruling_response.html"
        if not fixture_path.exists():
            pytest.skip("LA fixture not available")
        raw_html = fixture_path.read_text()
        result = preprocess_html(raw_html)
        # After preprocessing, the result should be much shorter than the raw HTML
        assert len(result) < len(raw_html)
        # Should contain actual ruling content
        assert "Case Number" in result or "DEPARTMENT" in result

    def test_real_la_bh205_fixture(self) -> None:
        """Test preprocessing against the large BH205 LA fixture."""
        fixture_path = FIXTURES_DIR / "la_ruling_bh205.html"
        if not fixture_path.exists():
            pytest.skip("LA BH205 fixture not available")
        raw_html = fixture_path.read_text()
        result = preprocess_html(raw_html)
        # The preprocessed text should be dramatically smaller
        assert len(result) < len(raw_html) / 2

    def test_real_la_com_a_fixture(self) -> None:
        """Test preprocessing against the COM A LA fixture."""
        fixture_path = FIXTURES_DIR / "la_ruling_com_a.html"
        if not fixture_path.exists():
            pytest.skip("LA COM A fixture not available")
        raw_html = fixture_path.read_text()
        result = preprocess_html(raw_html)
        assert len(result) < len(raw_html)
        assert len(result) > 0


# ---------------------------------------------------------------------------
# Case number normalization
# ---------------------------------------------------------------------------


class TestNormalizeCaseNumber:
    def test_strips_county_prefix(self) -> None:
        assert _normalize_case_number("30-2024-01393434") == "2024-01393434"

    def test_preserves_la_format(self) -> None:
        assert _normalize_case_number("24NNCV02551") == "24NNCV02551"

    def test_strips_whitespace(self) -> None:
        assert _normalize_case_number("  24NNCV02551  ") == "24NNCV02551"


# ---------------------------------------------------------------------------
# _parse_response
# ---------------------------------------------------------------------------


class TestParseResponse:
    def test_happy_path(self) -> None:
        response_json = json.dumps(
            {
                "judge_name": "William Crowfoot",
                "hearing_date": "2026-03-02",
                "department": "3",
                "rulings": [
                    {
                        "case_number": "24NNCV02551",
                        "case_title": "Smith v. Jones",
                        "outcome": "granted",
                        "motion_type": "msj",
                        "parties": [
                            {"name": "John Smith", "role": "plaintiff"},
                            {"name": "Jane Jones", "role": "defendant"},
                        ],
                    }
                ],
            }
        )
        result = _parse_response(response_json, None)
        assert result is not None
        assert result.judge_name == "William Crowfoot"
        assert result.hearing_date == date(2026, 3, 2)
        assert result.department == "3"
        assert result.case_count == 1
        assert result.rulings[0].case_number == "24NNCV02551"
        assert result.rulings[0].case_title == "Smith v. Jones"
        assert result.rulings[0].outcome == "granted"
        assert result.rulings[0].motion_type == "msj"
        assert len(result.rulings[0].parties) == 2

    def test_metadata_overrides_model_output(self) -> None:
        response_json = json.dumps(
            {
                "judge_name": "Wrong Name",
                "hearing_date": "2026-03-02",
                "department": "X",
                "rulings": [],
            }
        )
        metadata = {"judge_name": "Correct Name", "department": "5"}
        result = _parse_response(response_json, metadata)
        assert result is not None
        assert result.judge_name == "Correct Name"
        assert result.department == "5"

    def test_strips_markdown_code_fences(self) -> None:
        inner = json.dumps(
            {
                "judge_name": "Test Judge",
                "hearing_date": None,
                "department": None,
                "rulings": [],
            }
        )
        response = f"```json\n{inner}\n```"
        result = _parse_response(response, None)
        assert result is not None
        assert result.judge_name == "Test Judge"

    def test_invalid_outcome_normalized_to_other(self) -> None:
        response_json = json.dumps(
            {
                "judge_name": None,
                "hearing_date": None,
                "department": None,
                "rulings": [
                    {
                        "case_number": "123",
                        "case_title": "A v. B",
                        "outcome": "partially_granted",  # invalid
                        "motion_type": "msj",
                        "parties": [],
                    }
                ],
            }
        )
        result = _parse_response(response_json, None)
        assert result is not None
        assert result.rulings[0].outcome == "other"

    def test_oc_case_number_normalization(self) -> None:
        response_json = json.dumps(
            {
                "judge_name": None,
                "hearing_date": None,
                "department": None,
                "rulings": [
                    {
                        "case_number": "30-2024-01393434",
                        "case_title": None,
                        "outcome": None,
                        "motion_type": None,
                        "parties": [],
                    }
                ],
            }
        )
        result = _parse_response(response_json, None)
        assert result is not None
        assert result.rulings[0].case_number == "2024-01393434"

    def test_malformed_json_returns_none(self) -> None:
        result = _parse_response("not valid json {{{", None)
        assert result is None

    def test_multiple_rulings(self) -> None:
        response_json = json.dumps(
            {
                "judge_name": "Test Judge",
                "hearing_date": "2026-03-02",
                "department": "3",
                "rulings": [
                    {
                        "case_number": "001",
                        "case_title": "A v. B",
                        "outcome": "granted",
                        "motion_type": "msj",
                        "parties": [],
                    },
                    {
                        "case_number": "002",
                        "case_title": "C v. D",
                        "outcome": "denied",
                        "motion_type": "demurrer",
                        "parties": [],
                    },
                ],
            }
        )
        result = _parse_response(response_json, None)
        assert result is not None
        assert result.case_count == 2
        assert result.rulings[0].outcome == "granted"
        assert result.rulings[1].outcome == "denied"

    def test_null_fields_handled(self) -> None:
        response_json = json.dumps(
            {
                "judge_name": None,
                "hearing_date": None,
                "department": None,
                "rulings": [
                    {
                        "case_number": None,
                        "case_title": None,
                        "outcome": None,
                        "motion_type": None,
                        "parties": [],
                    }
                ],
            }
        )
        result = _parse_response(response_json, None)
        assert result is not None
        assert result.judge_name is None
        assert result.hearing_date is None
        assert result.rulings[0].case_number is None

    def test_invalid_date_returns_none(self) -> None:
        response_json = json.dumps(
            {
                "judge_name": None,
                "hearing_date": "not-a-date",
                "department": None,
                "rulings": [],
            }
        )
        result = _parse_response(response_json, None)
        assert result is not None
        assert result.hearing_date is None

    def test_rulings_not_a_list_handled(self) -> None:
        response_json = json.dumps(
            {
                "judge_name": None,
                "hearing_date": None,
                "department": None,
                "rulings": "not a list",
            }
        )
        result = _parse_response(response_json, None)
        assert result is not None
        assert result.rulings == []
        assert result.case_count == 0


# ---------------------------------------------------------------------------
# extract_fields_llm — integration tests with mocked API
# ---------------------------------------------------------------------------


class TestExtractFieldsLlm:
    """Tests for the top-level extract_fields_llm function."""

    def test_happy_path_pdf(self) -> None:
        response_json = json.dumps(
            {
                "judge_name": "Bobby Luna",
                "hearing_date": "2026-03-04",
                "department": "S22",
                "rulings": [
                    {
                        "case_number": "CIVSB2416631",
                        "case_title": "Acme v. Widget Co.",
                        "outcome": "granted",
                        "motion_type": "msj",
                        "parties": [
                            {"name": "Acme Corp", "role": "plaintiff"},
                            {"name": "Widget Co.", "role": "defendant"},
                        ],
                    }
                ],
            }
        )
        client = _mock_client(response_json)
        result = extract_fields_llm(
            document_text="Some PDF text about a ruling...",
            content_format="pdf",
            client=client,
        )
        assert result is not None
        assert result.judge_name == "Bobby Luna"
        assert result.case_count == 1
        assert result.rulings[0].outcome == "granted"

        # Verify the API was called with correct params
        call_args = client.messages.create.call_args
        assert call_args.kwargs["model"] == "claude-3-5-haiku-latest"
        assert call_args.kwargs["temperature"] == 0

    def test_happy_path_html(self) -> None:
        html_doc = """
        <html><body>
        <td><div id="speechSynthesis">
            <b>DEPARTMENT 3</b> Case Number: 24NNCV02551
            <p>Smith v. Jones</p>
            <p>Motion for summary judgment is GRANTED.</p>
        </div></td>
        </body></html>
        """
        response_json = json.dumps(
            {
                "judge_name": "Test Judge",
                "hearing_date": "2026-03-02",
                "department": "3",
                "rulings": [
                    {
                        "case_number": "24NNCV02551",
                        "case_title": "Smith v. Jones",
                        "outcome": "granted",
                        "motion_type": "msj",
                        "parties": [],
                    }
                ],
            }
        )
        client = _mock_client(response_json)
        result = extract_fields_llm(
            document_text=html_doc,
            content_format="html",
            client=client,
        )
        assert result is not None
        # Verify HTML was preprocessed — the user message should have stripped HTML
        user_msg = client.messages.create.call_args.kwargs["messages"][0]["content"]
        assert "<div" not in user_msg
        assert "DEPARTMENT 3" in user_msg

    def test_metadata_passed_to_model(self) -> None:
        response_json = json.dumps(
            {
                "judge_name": "Model Judge",
                "hearing_date": None,
                "department": None,
                "rulings": [],
            }
        )
        client = _mock_client(response_json)
        metadata = {"judge_name": "Auth Judge", "department": "5", "link_text": "Dept 5 rulings"}
        result = extract_fields_llm(
            document_text="Ruling text",
            content_format="pdf",
            metadata=metadata,
            client=client,
        )
        assert result is not None
        # Metadata should override model output
        assert result.judge_name == "Auth Judge"
        assert result.department == "5"
        # Metadata should appear in the user message
        user_msg = client.messages.create.call_args.kwargs["messages"][0]["content"]
        assert "Judge name (authoritative): Auth Judge" in user_msg

    def test_empty_text_returns_none(self) -> None:
        result = extract_fields_llm(
            document_text="",
            content_format="pdf",
            client=MagicMock(spec=anthropic.Anthropic),
        )
        assert result is None

    def test_whitespace_only_returns_none(self) -> None:
        result = extract_fields_llm(
            document_text="   \n\n  ",
            content_format="pdf",
            client=MagicMock(spec=anthropic.Anthropic),
        )
        assert result is None

    def test_api_error_returns_none(self) -> None:
        client = MagicMock(spec=anthropic.Anthropic)
        client.messages.create.side_effect = anthropic.APIError(
            message="Server error",
            request=MagicMock(),
            body=None,
        )
        result = extract_fields_llm(
            document_text="Some text",
            content_format="pdf",
            client=client,
        )
        assert result is None

    def test_rate_limit_retries_once(self) -> None:
        """On rate limit, should retry once with 1s backoff then succeed."""
        good_response = json.dumps(
            {
                "judge_name": None,
                "hearing_date": None,
                "department": None,
                "rulings": [],
            }
        )
        client = MagicMock(spec=anthropic.Anthropic)
        # First call raises rate limit, second succeeds
        rate_limit_error = anthropic.RateLimitError(
            message="Rate limited",
            response=MagicMock(status_code=429, headers={}),
            body=None,
        )
        client.messages.create.side_effect = [
            rate_limit_error,
            _make_api_response(good_response),
        ]
        with patch("ingestion.llm_extract.time.sleep") as mock_sleep:
            result = extract_fields_llm(
                document_text="Some text",
                content_format="pdf",
                client=client,
            )
        assert result is not None
        # Should have slept 1s between retries
        mock_sleep.assert_called_once_with(1)
        # Should have called the API twice
        assert client.messages.create.call_count == 2

    def test_rate_limit_exhausted_returns_none(self) -> None:
        """On double rate limit, should give up and return None."""
        client = MagicMock(spec=anthropic.Anthropic)
        rate_limit_error = anthropic.RateLimitError(
            message="Rate limited",
            response=MagicMock(status_code=429, headers={}),
            body=None,
        )
        client.messages.create.side_effect = rate_limit_error
        with patch("ingestion.llm_extract.time.sleep"):
            result = extract_fields_llm(
                document_text="Some text",
                content_format="pdf",
                client=client,
            )
        assert result is None

    def test_malformed_json_response_returns_none(self) -> None:
        client = _mock_client("This is not JSON at all")
        result = extract_fields_llm(
            document_text="Some text",
            content_format="pdf",
            client=client,
        )
        assert result is None

    def test_text_truncation(self) -> None:
        """Documents longer than 100K chars should be truncated."""
        long_text = "A" * 200_000
        response_json = json.dumps(
            {
                "judge_name": None,
                "hearing_date": None,
                "department": None,
                "rulings": [],
            }
        )
        client = _mock_client(response_json)
        result = extract_fields_llm(
            document_text=long_text,
            content_format="pdf",
            client=client,
        )
        assert result is not None
        # Verify the text sent to API was truncated
        user_msg = client.messages.create.call_args.kwargs["messages"][0]["content"]
        # The user message includes the "Document (pdf):" prefix, but the actual
        # text content should be <= 100K
        assert len(user_msg) < 200_000

    def test_client_created_from_env_when_not_provided(self) -> None:
        """When no client is provided, should create one from ANTHROPIC_API_KEY."""
        response_json = json.dumps(
            {
                "judge_name": None,
                "hearing_date": None,
                "department": None,
                "rulings": [],
            }
        )
        mock_instance = _mock_client(response_json)

        with patch("ingestion.llm_extract.anthropic.Anthropic", return_value=mock_instance):
            result = extract_fields_llm(
                document_text="Some text",
                content_format="pdf",
                # No client passed
            )
        assert result is not None


# ---------------------------------------------------------------------------
# Real fixture tests (mocked API, real HTML preprocessing)
# ---------------------------------------------------------------------------


class TestRealFixtures:
    """Tests using real court ruling fixtures with mocked API responses.

    These verify that HTML preprocessing correctly extracts content from
    real fixture files, and that the module handles realistic data shapes.
    """

    def test_la_ruling_response_preprocessing(self) -> None:
        """Verify LA ruling HTML is preprocessed and sent to the API."""
        fixture_path = FIXTURES_DIR / "la_ruling_response.html"
        if not fixture_path.exists():
            pytest.skip("LA fixture not available")

        raw_html = fixture_path.read_text()
        response_json = json.dumps(
            {
                "judge_name": "William Crowfoot",
                "hearing_date": "2026-03-02",
                "department": "3",
                "rulings": [
                    {
                        "case_number": "24NNCV02551",
                        "case_title": "Plaintiff v. Defendant",
                        "outcome": "granted",
                        "motion_type": "msj",
                        "parties": [],
                    }
                ],
            }
        )
        client = _mock_client(response_json)
        result = extract_fields_llm(
            document_text=raw_html,
            content_format="html",
            client=client,
        )
        assert result is not None
        assert result.case_count == 1

        # The user message should have been preprocessed (no raw HTML tags)
        user_msg = client.messages.create.call_args.kwargs["messages"][0]["content"]
        assert "<div" not in user_msg
        # But should contain the actual ruling text
        assert "Case Number" in user_msg or "DEPARTMENT" in user_msg

    def test_la_dept_header_fixture(self) -> None:
        """Test with the LA dept header fixture."""
        fixture_path = FIXTURES_DIR / "la_ruling_dept_header.html"
        if not fixture_path.exists():
            pytest.skip("LA dept header fixture not available")

        raw_html = fixture_path.read_text()
        response_json = json.dumps(
            {
                "judge_name": "Steven Ellis",
                "hearing_date": "2026-03-03",
                "department": "56",
                "rulings": [
                    {
                        "case_number": "23STCV12345",
                        "case_title": "Test v. Case",
                        "outcome": "denied",
                        "motion_type": "demurrer",
                        "parties": [
                            {"name": "Test Plaintiff", "role": "plaintiff"},
                            {"name": "Test Defendant", "role": "defendant"},
                        ],
                    }
                ],
            }
        )
        client = _mock_client(response_json)
        result = extract_fields_llm(
            document_text=raw_html,
            content_format="html",
            metadata={"judge_name": "Steven A. Ellis", "department": "56"},
            client=client,
        )
        assert result is not None
        # Metadata should override
        assert result.judge_name == "Steven A. Ellis"
        assert result.department == "56"
        assert result.case_count == 1
        assert result.rulings[0].outcome == "denied"

    def test_pdf_fixture_passthrough(self) -> None:
        """PDF text should be passed through without HTML preprocessing."""
        # Simulate PDF-extracted text (as would come from pdfplumber)
        pdf_text = """
        Department S22 - Judge Bobby P. Luna
        San Bernardino County Superior Court

        Case Number: CIVSB2416631
        Case Title: Acme Corp v. Widget Co.
        Hearing Date: March 4, 2026

        Motion for Summary Judgment: GRANTED

        The court finds that there are no triable issues of material fact.
        """
        response_json = json.dumps(
            {
                "judge_name": "Bobby P. Luna",
                "hearing_date": "2026-03-04",
                "department": "S22",
                "rulings": [
                    {
                        "case_number": "CIVSB2416631",
                        "case_title": "Acme Corp v. Widget Co.",
                        "outcome": "granted",
                        "motion_type": "msj",
                        "parties": [
                            {"name": "Acme Corp", "role": "plaintiff"},
                            {"name": "Widget Co.", "role": "defendant"},
                        ],
                    }
                ],
            }
        )
        client = _mock_client(response_json)
        result = extract_fields_llm(
            document_text=pdf_text,
            content_format="pdf",
            client=client,
        )
        assert result is not None
        assert result.judge_name == "Bobby P. Luna"
        assert result.hearing_date == date(2026, 3, 4)
        assert result.case_count == 1

        # PDF text should be passed as-is (no HTML stripping)
        user_msg = client.messages.create.call_args.kwargs["messages"][0]["content"]
        assert "Department S22 - Judge Bobby P. Luna" in user_msg
