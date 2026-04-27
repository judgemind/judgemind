"""Tests for LLM-based field extraction (llm_extract.py).

All tests mock the LLM provider layer — no real API calls are made.
"""

from __future__ import annotations

import json
import threading
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

from framework.llm_schema import ConfidenceLevel, FieldConfidence
from ingestion.llm_extract import (
    _SYSTEM_PROMPT,
    CASE_TYPE_VALUES,
    LLMExtractionResult,
    LLMRulingResult,
    TokenTracker,
    _deserialize_result,
    _merge_results,
    _normalize_case_number,
    _normalize_department,
    _parse_confidence,
    _parse_response,
    _serialize_result,
    _split_text_into_chunks,
    extract_fields_llm,
    preprocess_html,
)
from ingestion.llm_providers import LLMResponse

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).parent / "fixtures"


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
        assert fixture_path.exists(), f"Fixture missing: {fixture_path}"
        raw_html = fixture_path.read_text()
        result = preprocess_html(raw_html)
        # After preprocessing, the result should be much shorter than the raw HTML
        assert len(result) < len(raw_html)
        # Should contain actual ruling content
        assert "Case Number" in result or "DEPARTMENT" in result

    def test_real_la_bh205_fixture(self) -> None:
        """Test preprocessing against the large BH205 LA fixture."""
        fixture_path = FIXTURES_DIR / "la_ruling_bh205.html"
        assert fixture_path.exists(), f"Fixture missing: {fixture_path}"
        raw_html = fixture_path.read_text()
        result = preprocess_html(raw_html)
        # The preprocessed text should be dramatically smaller
        assert len(result) < len(raw_html) / 2

    def test_real_la_com_a_fixture(self) -> None:
        """Test preprocessing against the COM A LA fixture."""
        fixture_path = FIXTURES_DIR / "la_ruling_com_a.html"
        assert fixture_path.exists(), f"Fixture missing: {fixture_path}"
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
# _normalize_department
# ---------------------------------------------------------------------------


class TestNormalizeDepartment:
    """Tests for department leading-zero normalization."""

    def test_cm02_from_cm2(self) -> None:
        """LLM returns 'CM2' — should be normalized to 'CM02'."""
        assert _normalize_department("CM2") == "CM02"

    def test_cm05_from_cm5(self) -> None:
        """LLM returns 'CM5' — should be normalized to 'CM05'."""
        assert _normalize_department("CM5") == "CM05"

    def test_cm02_already_correct(self) -> None:
        """Already-correct 'CM02' should be unchanged."""
        assert _normalize_department("CM02") == "CM02"

    def test_cm12_two_digits_unchanged(self) -> None:
        """Two-digit suffix 'CM12' should be unchanged."""
        assert _normalize_department("CM12") == "CM12"

    def test_cx_prefix(self) -> None:
        """CX prefix should also be zero-padded."""
        assert _normalize_department("CX3") == "CX03"

    def test_l_prefix(self) -> None:
        """L prefix (Lamoreaux) should be zero-padded."""
        assert _normalize_department("L5") == "L05"

    def test_unknown_prefix_unchanged(self) -> None:
        """Unknown prefix 'N14' should be unchanged (not in mapping)."""
        assert _normalize_department("N14") == "N14"

    def test_unknown_prefix_single_digit_unchanged(self) -> None:
        """Unknown prefix 'Z3' should be unchanged (not in mapping)."""
        assert _normalize_department("Z3") == "Z3"

    def test_no_digits_unchanged(self) -> None:
        """Pure text department like 'A' should be unchanged."""
        assert _normalize_department("A") == "A"

    def test_pure_digits_unchanged(self) -> None:
        """Pure numeric department like '25' should be unchanged."""
        assert _normalize_department("25") == "25"

    def test_strips_whitespace(self) -> None:
        """Leading/trailing whitespace should be stripped."""
        assert _normalize_department("  CM2  ") == "CM02"

    def test_preserves_original_case(self) -> None:
        """Lowercase input prefix should preserve its casing."""
        assert _normalize_department("cm2") == "cm02"

    # Hyphen removal (#2123)

    def test_strips_hyphen_s17(self) -> None:
        """LLM returns 'S-17' — hyphen should be stripped to 'S17'."""
        assert _normalize_department("S-17") == "S17"

    def test_strips_hyphen_r14(self) -> None:
        """LLM returns 'R-14' — hyphen should be stripped to 'R14'."""
        assert _normalize_department("R-14") == "R14"

    def test_strips_hyphen_s22(self) -> None:
        """LLM returns 'S-22' — hyphen should be stripped to 'S22'."""
        assert _normalize_department("S-22") == "S22"

    def test_strips_hyphen_with_whitespace(self) -> None:
        """Hyphenated with surrounding whitespace should normalize."""
        assert _normalize_department("  S-17  ") == "S17"

    def test_strips_hyphen_then_zero_pads(self) -> None:
        """'CM-2' should become 'CM02' (hyphen stripped, then zero-padded)."""
        assert _normalize_department("CM-2") == "CM02"

    def test_strips_hyphen_preserves_case(self) -> None:
        """Lowercase 's-17' should become 's17' preserving original casing."""
        assert _normalize_department("s-17") == "s17"

    def test_no_hyphen_unchanged(self) -> None:
        """Already non-hyphenated 'S17' should be unchanged."""
        assert _normalize_department("S17") == "S17"

    def test_multi_letter_prefix_hyphen(self) -> None:
        """Multi-letter prefix with hyphen: 'CM-12' should become 'CM12'."""
        assert _normalize_department("CM-12") == "CM12"


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

    def test_department_leading_zero_normalized_without_metadata(self) -> None:
        """When no metadata, LLM output 'CM2' should be normalized to 'CM02'."""
        response_json = json.dumps(
            {
                "judge_name": None,
                "hearing_date": None,
                "department": "CM2",
                "rulings": [],
            }
        )
        result = _parse_response(response_json, None)
        assert result is not None
        assert result.department == "CM02"

    def test_department_metadata_overrides_normalization(self) -> None:
        """Metadata department should take precedence even over normalization."""
        response_json = json.dumps(
            {
                "judge_name": None,
                "hearing_date": None,
                "department": "CM2",
                "rulings": [],
            }
        )
        metadata = {"department": "CM02"}
        result = _parse_response(response_json, metadata)
        assert result is not None
        assert result.department == "CM02"

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

    def test_long_party_name_discarded(self) -> None:
        """Party names > 200 chars are discarded as garbage."""
        long_name = "A" * 300
        response_json = json.dumps(
            {
                "judge_name": None,
                "hearing_date": None,
                "department": None,
                "rulings": [
                    {
                        "case_number": "001",
                        "case_title": "A v. B",
                        "outcome": "granted",
                        "motion_type": "msj",
                        "parties": [
                            {"name": long_name, "role": "plaintiff"},
                            {"name": "Valid Name", "role": "defendant"},
                        ],
                    }
                ],
            }
        )
        result = _parse_response(response_json, None)
        assert result is not None
        # Only the valid party should remain
        assert len(result.rulings[0].parties) == 1
        assert result.rulings[0].parties[0]["name"] == "Valid Name"

    def test_party_name_with_newlines_discarded(self) -> None:
        """Party names containing newlines are discarded as garbage."""
        response_json = json.dumps(
            {
                "judge_name": None,
                "hearing_date": None,
                "department": None,
                "rulings": [
                    {
                        "case_number": "001",
                        "case_title": "A v. B",
                        "outcome": "granted",
                        "motion_type": "msj",
                        "parties": [
                            {"name": "Line one\nLine two", "role": "plaintiff"},
                            {"name": "Good Name", "role": "defendant"},
                        ],
                    }
                ],
            }
        )
        result = _parse_response(response_json, None)
        assert result is not None
        assert len(result.rulings[0].parties) == 1
        assert result.rulings[0].parties[0]["name"] == "Good Name"

    def test_party_name_with_carriage_return_discarded(self) -> None:
        """Party names containing \\r are discarded."""
        response_json = json.dumps(
            {
                "judge_name": None,
                "hearing_date": None,
                "department": None,
                "rulings": [
                    {
                        "case_number": "001",
                        "case_title": "X v. Y",
                        "outcome": None,
                        "motion_type": None,
                        "parties": [
                            {"name": "Bad\rName", "role": "plaintiff"},
                        ],
                    }
                ],
            }
        )
        result = _parse_response(response_json, None)
        assert result is not None
        assert len(result.rulings[0].parties) == 0

    def test_case_type_extracted(self) -> None:
        """Valid case_type values are extracted and preserved."""
        response_json = json.dumps(
            {
                "judge_name": None,
                "hearing_date": None,
                "department": None,
                "rulings": [
                    {
                        "case_number": "24STCV12345",
                        "case_title": "Smith v. Jones",
                        "case_type": "civil",
                        "outcome": "granted",
                        "motion_type": "msj",
                        "parties": [],
                    }
                ],
            }
        )
        result = _parse_response(response_json, None)
        assert result is not None
        assert result.rulings[0].case_type == "civil"

    def test_case_type_all_valid_values(self) -> None:
        """All defined CASE_TYPE_VALUES are accepted without normalization."""
        for case_type in CASE_TYPE_VALUES:
            response_json = json.dumps(
                {
                    "judge_name": None,
                    "hearing_date": None,
                    "department": None,
                    "rulings": [
                        {
                            "case_number": "001",
                            "case_title": "A v. B",
                            "case_type": case_type,
                            "outcome": None,
                            "motion_type": None,
                            "parties": [],
                        }
                    ],
                }
            )
            result = _parse_response(response_json, None)
            assert result is not None
            assert result.rulings[0].case_type == case_type

    def test_case_type_invalid_normalized_to_other(self) -> None:
        """Invalid case_type values are normalized to 'other'."""
        response_json = json.dumps(
            {
                "judge_name": None,
                "hearing_date": None,
                "department": None,
                "rulings": [
                    {
                        "case_number": "001",
                        "case_title": "A v. B",
                        "case_type": "administrative",  # not in enum
                        "outcome": None,
                        "motion_type": None,
                        "parties": [],
                    }
                ],
            }
        )
        result = _parse_response(response_json, None)
        assert result is not None
        assert result.rulings[0].case_type == "other"

    def test_case_type_null_preserved(self) -> None:
        """Null case_type is preserved as None."""
        response_json = json.dumps(
            {
                "judge_name": None,
                "hearing_date": None,
                "department": None,
                "rulings": [
                    {
                        "case_number": "001",
                        "case_title": "A v. B",
                        "case_type": None,
                        "outcome": None,
                        "motion_type": None,
                        "parties": [],
                    }
                ],
            }
        )
        result = _parse_response(response_json, None)
        assert result is not None
        assert result.rulings[0].case_type is None

    def test_case_type_missing_field_defaults_to_none(self) -> None:
        """When case_type is not in the response, it defaults to None."""
        response_json = json.dumps(
            {
                "judge_name": None,
                "hearing_date": None,
                "department": None,
                "rulings": [
                    {
                        "case_number": "001",
                        "case_title": "A v. B",
                        "outcome": None,
                        "motion_type": None,
                        "parties": [],
                    }
                ],
            }
        )
        result = _parse_response(response_json, None)
        assert result is not None
        assert result.rulings[0].case_type is None

    def test_valid_party_names_preserved(self) -> None:
        """Normal-length party names without newlines pass validation."""
        response_json = json.dumps(
            {
                "judge_name": None,
                "hearing_date": None,
                "department": None,
                "rulings": [
                    {
                        "case_number": "001",
                        "case_title": "A v. B",
                        "outcome": "granted",
                        "motion_type": "msj",
                        "parties": [
                            {"name": "John Smith", "role": "plaintiff"},
                            {"name": "Acme Corp International Holdings LLC", "role": "defendant"},
                        ],
                    }
                ],
            }
        )
        result = _parse_response(response_json, None)
        assert result is not None
        assert len(result.rulings[0].parties) == 2


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
        mock_fn = _mock_call_llm(response_json)
        with patch("ingestion.llm_extract.call_llm", mock_fn):
            result = extract_fields_llm(
                document_text="Some PDF text about a ruling...",
                content_format="pdf",
            )
        assert result is not None
        assert result.judge_name == "Bobby Luna"
        assert result.case_count == 1
        assert result.rulings[0].outcome == "granted"

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
        mock_fn = _mock_call_llm(response_json)
        with patch("ingestion.llm_extract.call_llm", mock_fn):
            result = extract_fields_llm(
                document_text=html_doc,
                content_format="html",
            )
        assert result is not None
        # Verify HTML was preprocessed — the user message should not have HTML tags
        call_args = mock_fn.call_args
        user_msg = call_args.kwargs["user_message"]
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
        mock_fn = _mock_call_llm(response_json)
        metadata = {"judge_name": "Auth Judge", "department": "5", "link_text": "Dept 5 rulings"}
        with patch("ingestion.llm_extract.call_llm", mock_fn):
            result = extract_fields_llm(
                document_text="Ruling text",
                content_format="pdf",
                metadata=metadata,
            )
        assert result is not None
        # Metadata should override model output
        assert result.judge_name == "Auth Judge"
        assert result.department == "5"
        # Metadata should appear in the user message
        call_args = mock_fn.call_args
        user_msg = call_args.kwargs["user_message"]
        assert "Judge name (authoritative): Auth Judge" in user_msg

    def test_case_type_in_system_prompt(self) -> None:
        """The system prompt should instruct the LLM about case_type extraction."""
        response_json = json.dumps(
            {
                "judge_name": None,
                "hearing_date": None,
                "department": None,
                "rulings": [
                    {
                        "case_number": "24STCV12345",
                        "case_title": "Smith v. Jones",
                        "case_type": "civil",
                        "outcome": "granted",
                        "motion_type": "msj",
                        "parties": [],
                    }
                ],
            }
        )
        mock_fn = _mock_call_llm(response_json)
        with patch("ingestion.llm_extract.call_llm", mock_fn):
            extract_fields_llm(
                document_text="Some ruling text",
                content_format="pdf",
            )
        call_kwargs = mock_fn.call_args.kwargs
        system_prompt = call_kwargs["system_prompt"]
        assert "case_type" in system_prompt
        assert "civil" in system_prompt
        assert "small_claims" in system_prompt

    def test_empty_text_returns_none(self) -> None:
        result = extract_fields_llm(
            document_text="",
            content_format="pdf",
        )
        assert result is None

    def test_whitespace_only_returns_none(self) -> None:
        result = extract_fields_llm(
            document_text="   \n\n  ",
            content_format="pdf",
        )
        assert result is None

    def test_api_error_returns_none(self) -> None:
        mock_fn = MagicMock(return_value=None)
        with patch("ingestion.llm_extract.call_llm", mock_fn):
            result = extract_fields_llm(
                document_text="Some text",
                content_format="pdf",
            )
        assert result is None

    def test_malformed_json_response_returns_none(self) -> None:
        mock_fn = _mock_call_llm("This is not JSON at all")
        with patch("ingestion.llm_extract.call_llm", mock_fn):
            result = extract_fields_llm(
                document_text="Some text",
                content_format="pdf",
            )
        assert result is None

    def test_small_doc_no_chunking(self) -> None:
        """Documents under max_chars should be processed in a single call."""
        short_text = "A" * 1000
        response_json = json.dumps(
            {
                "judge_name": None,
                "hearing_date": None,
                "department": None,
                "rulings": [],
            }
        )
        mock_fn = _mock_call_llm(response_json)
        with patch("ingestion.llm_extract.call_llm", mock_fn):
            result = extract_fields_llm(
                document_text=short_text,
                content_format="pdf",
                max_chars=80_000,
            )
        assert result is not None
        assert mock_fn.call_count == 1

    def test_provider_and_model_passed_through(self) -> None:
        """Provider and model args are forwarded to call_llm."""
        response_json = json.dumps(
            {
                "judge_name": None,
                "hearing_date": None,
                "department": None,
                "rulings": [],
            }
        )
        mock_fn = _mock_call_llm(response_json)
        with patch("ingestion.llm_extract.call_llm", mock_fn):
            result = extract_fields_llm(
                document_text="Some text",
                content_format="pdf",
                provider="google",
                model="gemini-2.5-flash-lite",
            )
        assert result is not None
        call_kwargs = mock_fn.call_args.kwargs
        assert call_kwargs["provider"] == "google"
        assert call_kwargs["model"] == "gemini-2.5-flash-lite"

    def test_timeout_forwarded_to_call_llm(self) -> None:
        """The timeout parameter is forwarded to call_llm."""
        response_json = json.dumps(
            {
                "judge_name": "Test",
                "hearing_date": None,
                "department": None,
                "rulings": [],
            }
        )
        mock_fn = _mock_call_llm(response_json)
        with patch("ingestion.llm_extract.call_llm", mock_fn):
            extract_fields_llm(
                document_text="Some text",
                content_format="pdf",
                timeout=42.0,
            )
        call_kwargs = mock_fn.call_args.kwargs
        assert call_kwargs["timeout"] == 42.0

    def test_timeout_none_by_default(self) -> None:
        """When timeout is not specified, None is forwarded."""
        response_json = json.dumps(
            {
                "judge_name": "Test",
                "hearing_date": None,
                "department": None,
                "rulings": [],
            }
        )
        mock_fn = _mock_call_llm(response_json)
        with patch("ingestion.llm_extract.call_llm", mock_fn):
            extract_fields_llm(
                document_text="Some text",
                content_format="pdf",
            )
        call_kwargs = mock_fn.call_args.kwargs
        assert call_kwargs["timeout"] is None

    def test_timeout_triggers_fallback_to_none(self) -> None:
        """When call_llm returns None (e.g. timeout), extract_fields_llm returns None."""
        mock_fn = MagicMock(return_value=None)
        with patch("ingestion.llm_extract.call_llm", mock_fn):
            result = extract_fields_llm(
                document_text="Some text about a ruling",
                content_format="pdf",
                timeout=10.0,
            )
        assert result is None


# ---------------------------------------------------------------------------
# Real fixture tests (mocked API, real HTML preprocessing)
# ---------------------------------------------------------------------------


class TestRealFixtures:
    """Tests using real court ruling fixtures with mocked LLM responses.

    These verify that HTML preprocessing correctly extracts content from
    real fixture files, and that the module handles realistic data shapes.
    """

    def test_la_ruling_response_preprocessing(self) -> None:
        """Verify LA ruling HTML is preprocessed and sent to the LLM."""
        fixture_path = FIXTURES_DIR / "la_ruling_response.html"
        assert fixture_path.exists(), f"Fixture missing: {fixture_path}"

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
        mock_fn = _mock_call_llm(response_json)
        with patch("ingestion.llm_extract.call_llm", mock_fn):
            result = extract_fields_llm(
                document_text=raw_html,
                content_format="html",
            )
        assert result is not None
        assert result.case_count == 1

        # The user message should have been preprocessed (no raw HTML tags)
        user_msg = mock_fn.call_args.kwargs["user_message"]
        assert "<div" not in user_msg
        # But should contain the actual ruling text
        assert "Case Number" in user_msg or "DEPARTMENT" in user_msg

    def test_la_dept_header_fixture(self) -> None:
        """Test with the LA dept header fixture."""
        fixture_path = FIXTURES_DIR / "la_ruling_dept_header.html"
        assert fixture_path.exists(), f"Fixture missing: {fixture_path}"

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
        mock_fn = _mock_call_llm(response_json)
        with patch("ingestion.llm_extract.call_llm", mock_fn):
            result = extract_fields_llm(
                document_text=raw_html,
                content_format="html",
                metadata={"judge_name": "Steven A. Ellis", "department": "56"},
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
        mock_fn = _mock_call_llm(response_json)
        with patch("ingestion.llm_extract.call_llm", mock_fn):
            result = extract_fields_llm(
                document_text=pdf_text,
                content_format="pdf",
            )
        assert result is not None
        assert result.judge_name == "Bobby P. Luna"
        assert result.hearing_date == date(2026, 3, 4)
        assert result.case_count == 1

        # PDF text should be passed as-is (no HTML stripping)
        user_msg = mock_fn.call_args.kwargs["user_message"]
        assert "Department S22 - Judge Bobby P. Luna" in user_msg


# ---------------------------------------------------------------------------
# Chunking tests
# ---------------------------------------------------------------------------


class TestSplitTextIntoChunks:
    """Tests for _split_text_into_chunks."""

    def test_short_text_returns_single_chunk(self) -> None:
        text = "Short document"
        chunks = _split_text_into_chunks(text, max_chars=1000, content_format="pdf")
        assert chunks == [text]

    def test_pdf_splits_on_form_feed(self) -> None:
        page1 = "A" * 5000
        page2 = "B" * 5000
        page3 = "C" * 5000
        text = f"{page1}\f{page2}\f{page3}"
        # max_chars=6000 means page1 fits alone, page2+page3 must split
        chunks = _split_text_into_chunks(text, max_chars=6000, content_format="pdf")
        assert len(chunks) >= 2
        # First chunk should contain page1 content
        assert "A" * 100 in chunks[0]

    def test_html_splits_on_case_boundary(self) -> None:
        case1 = "Case Number: 001\nSome ruling text for case 1\n"
        case2 = "Case Number: 002\nSome ruling text for case 2\n"
        text = case1 + "\n" + case2
        # Set max_chars small enough to force a split
        chunks = _split_text_into_chunks(text, max_chars=40, content_format="html")
        assert len(chunks) >= 2

    def test_fallback_to_paragraph_breaks(self) -> None:
        # No form feeds, no HR, no case numbers — just paragraphs
        paragraphs = ["Paragraph " + str(i) + " text." for i in range(20)]
        text = "\n\n".join(paragraphs)
        chunks = _split_text_into_chunks(text, max_chars=100, content_format="pdf")
        assert len(chunks) >= 2

    def test_force_split_wall_of_text(self) -> None:
        # No natural boundaries at all
        text = "A" * 10000
        chunks = _split_text_into_chunks(text, max_chars=3000, content_format="pdf")
        assert len(chunks) >= 2
        # Each chunk should be at most max_chars
        for chunk in chunks:
            assert len(chunk) <= 3000 + 500  # allow for overlap prefix

    def test_max_chunks_cap(self) -> None:
        # Create a very long document that would need many chunks
        text = "A" * 500_000
        chunks = _split_text_into_chunks(text, max_chars=3000, content_format="pdf")
        assert len(chunks) <= 10

    def test_overlap_between_chunks(self) -> None:
        # Force-split a wall of text and verify overlap
        text = "ABCDEFGHIJ" * 1000  # 10K chars
        chunks = _split_text_into_chunks(text, max_chars=3000, content_format="pdf")
        if len(chunks) >= 2:
            # The end of chunk[0] should appear at the start of chunk[1]
            tail_of_first = chunks[0][-500:]
            assert tail_of_first in chunks[1]


class TestMergeResults:
    """Tests for _merge_results."""

    def test_empty_list(self) -> None:
        result = _merge_results([])
        assert result.case_count == 0
        assert result.rulings == []

    def test_single_result_passthrough(self) -> None:
        r = LLMExtractionResult(
            judge_name="Judge A",
            hearing_date=date(2026, 3, 9),
            department="5",
            case_count=1,
            rulings=[LLMRulingResult(case_number="001", outcome="granted")],
        )
        merged = _merge_results([r])
        assert merged.judge_name == "Judge A"
        assert merged.case_count == 1

    def test_doc_fields_from_first_chunk(self) -> None:
        r1 = LLMExtractionResult(
            judge_name="Judge First",
            hearing_date=date(2026, 3, 9),
            department="A",
            case_count=1,
            rulings=[LLMRulingResult(case_number="001")],
        )
        r2 = LLMExtractionResult(
            judge_name="Judge Second",
            hearing_date=date(2026, 3, 10),
            department="B",
            case_count=1,
            rulings=[LLMRulingResult(case_number="002")],
        )
        merged = _merge_results([r1, r2])
        assert merged.judge_name == "Judge First"
        assert merged.hearing_date == date(2026, 3, 9)
        assert merged.department == "A"

    def test_deduplication_by_case_number(self) -> None:
        r1 = LLMExtractionResult(
            judge_name="Judge",
            rulings=[
                LLMRulingResult(case_number="001", outcome="granted"),
                LLMRulingResult(case_number="002", outcome="denied"),
            ],
        )
        r2 = LLMExtractionResult(
            judge_name="Judge",
            rulings=[
                LLMRulingResult(case_number="002", outcome="denied"),  # dup
                LLMRulingResult(case_number="003", outcome="moot"),
            ],
        )
        merged = _merge_results([r1, r2])
        assert merged.case_count == 3
        case_numbers = [r.case_number for r in merged.rulings]
        assert case_numbers == ["001", "002", "003"]

    def test_rulings_without_case_number_not_deduped(self) -> None:
        """Rulings with None case_number should all be kept."""
        r1 = LLMExtractionResult(
            rulings=[LLMRulingResult(case_number=None, outcome="granted")],
        )
        r2 = LLMExtractionResult(
            rulings=[LLMRulingResult(case_number=None, outcome="denied")],
        )
        merged = _merge_results([r1, r2])
        assert merged.case_count == 2

    def test_case_count_is_unique_rulings(self) -> None:
        """case_count should be count of unique rulings, not sum of per-chunk counts."""
        r1 = LLMExtractionResult(
            case_count=2,
            rulings=[
                LLMRulingResult(case_number="001"),
                LLMRulingResult(case_number="002"),
            ],
        )
        r2 = LLMExtractionResult(
            case_count=2,
            rulings=[
                LLMRulingResult(case_number="002"),  # overlap
                LLMRulingResult(case_number="003"),
            ],
        )
        merged = _merge_results([r1, r2])
        # Should be 3 unique, not 4 (sum)
        assert merged.case_count == 3


class TestChunkedExtraction:
    """Integration tests for chunked extraction through extract_fields_llm."""

    def test_large_doc_produces_multiple_calls(self) -> None:
        """A document exceeding max_chars should produce multiple LLM calls."""
        # Build a two-page PDF document where each page exceeds max_chars
        page1 = "Page 1 content. " * 5000  # ~80K chars
        page2 = "Page 2 content. " * 5000  # ~80K chars
        text = f"{page1}\f{page2}"

        response_chunk1 = json.dumps(
            {
                "judge_name": "Test Judge",
                "hearing_date": "2026-03-09",
                "department": "5",
                "rulings": [
                    {
                        "case_number": "001",
                        "case_title": "A v. B",
                        "outcome": "granted",
                        "motion_type": "msj",
                        "parties": [],
                    }
                ],
            }
        )
        response_chunk2 = json.dumps(
            {
                "judge_name": "Test Judge",
                "hearing_date": "2026-03-09",
                "department": "5",
                "rulings": [
                    {
                        "case_number": "002",
                        "case_title": "C v. D",
                        "outcome": "denied",
                        "motion_type": "demurrer",
                        "parties": [],
                    }
                ],
            }
        )

        # Alternate responses for however many chunks are produced
        responses = [response_chunk1, response_chunk2] * 3
        _call_counter = threading.local()
        _call_lock = threading.Lock()
        _call_count_shared = [0]

        def mock_call_llm_side_effect(**kwargs: object) -> LLMResponse:
            with _call_lock:
                idx = _call_count_shared[0] % len(responses)
                _call_count_shared[0] += 1
            return LLMResponse(text=responses[idx], input_tokens=100, output_tokens=50)

        mock_fn = MagicMock(side_effect=mock_call_llm_side_effect)
        with patch("ingestion.llm_extract.call_llm", mock_fn):
            result = extract_fields_llm(
                document_text=text,
                content_format="pdf",
                max_chars=80_000,
            )
        assert result is not None
        # Should have made more than 1 call
        assert mock_fn.call_count >= 2
        # Should have results from multiple chunks
        assert result.case_count >= 1
        assert result.judge_name == "Test Judge"

    def test_dedup_across_overlap_region(self) -> None:
        """Same case appearing in overlap region should be deduplicated."""
        response1 = json.dumps(
            {
                "judge_name": "Judge Overlap",
                "hearing_date": "2026-03-09",
                "department": "3",
                "rulings": [
                    {
                        "case_number": "OVERLAP-001",
                        "case_title": "X v. Y",
                        "outcome": "granted",
                        "motion_type": "msj",
                        "parties": [],
                    },
                    {
                        "case_number": "UNIQUE-001",
                        "case_title": "A v. B",
                        "outcome": "denied",
                        "motion_type": "demurrer",
                        "parties": [],
                    },
                ],
            }
        )
        response2 = json.dumps(
            {
                "judge_name": "Judge Overlap",
                "hearing_date": "2026-03-09",
                "department": "3",
                "rulings": [
                    {
                        "case_number": "OVERLAP-001",  # duplicate from overlap
                        "case_title": "X v. Y",
                        "outcome": "granted",
                        "motion_type": "msj",
                        "parties": [],
                    },
                    {
                        "case_number": "UNIQUE-002",
                        "case_title": "C v. D",
                        "outcome": "moot",
                        "motion_type": "mtd",
                        "parties": [],
                    },
                ],
            }
        )

        half = "X" * 50_000
        text = f"{half}\f{half}"

        responses = [response1, response2]
        _lock = threading.Lock()
        _idx = [0]

        def mock_side_effect(**kwargs: object) -> LLMResponse:
            with _lock:
                r = responses[min(_idx[0], len(responses) - 1)]
                _idx[0] += 1
            return LLMResponse(text=r, input_tokens=100, output_tokens=50)

        mock_fn = MagicMock(side_effect=mock_side_effect)
        with patch("ingestion.llm_extract.call_llm", mock_fn):
            result = extract_fields_llm(
                document_text=text,
                content_format="pdf",
                max_chars=60_000,
            )
        assert result is not None
        # OVERLAP-001 appears in both chunks but should only appear once
        assert result.case_count == 3
        case_numbers = [r.case_number for r in result.rulings]
        assert case_numbers == ["OVERLAP-001", "UNIQUE-001", "UNIQUE-002"]

    def test_merge_takes_doc_fields_from_first_chunk(self) -> None:
        """Document-level fields should come from the first chunk."""
        response1 = json.dumps(
            {
                "judge_name": "First Chunk Judge",
                "hearing_date": "2026-03-01",
                "department": "A",
                "rulings": [
                    {
                        "case_number": "001",
                        "case_title": "A v. B",
                        "outcome": "granted",
                        "motion_type": "msj",
                        "parties": [],
                    }
                ],
            }
        )
        response2 = json.dumps(
            {
                "judge_name": "Second Chunk Judge",
                "hearing_date": "2026-03-02",
                "department": "B",
                "rulings": [
                    {
                        "case_number": "002",
                        "case_title": "C v. D",
                        "outcome": "denied",
                        "motion_type": "demurrer",
                        "parties": [],
                    }
                ],
            }
        )

        half = "Y" * 50_000
        text = f"{half}\f{half}"

        responses = [response1, response2]
        _lock = threading.Lock()
        _idx = [0]

        def mock_side_effect(**kwargs: object) -> LLMResponse:
            with _lock:
                r = responses[min(_idx[0], len(responses) - 1)]
                _idx[0] += 1
            return LLMResponse(text=r, input_tokens=100, output_tokens=50)

        mock_fn = MagicMock(side_effect=mock_side_effect)
        with patch("ingestion.llm_extract.call_llm", mock_fn):
            result = extract_fields_llm(
                document_text=text,
                content_format="pdf",
                max_chars=60_000,
            )
        assert result is not None
        assert result.judge_name == "First Chunk Judge"
        assert result.hearing_date == date(2026, 3, 1)
        assert result.department == "A"
        assert result.case_count == 2

    def test_chunk_api_failure_partial_result(self) -> None:
        """If one chunk's LLM call fails, the other chunks' results are returned."""
        good_response = json.dumps(
            {
                "judge_name": "Good Judge",
                "hearing_date": "2026-03-09",
                "department": "1",
                "rulings": [
                    {
                        "case_number": "001",
                        "case_title": "A v. B",
                        "outcome": "granted",
                        "motion_type": "msj",
                        "parties": [],
                    }
                ],
            }
        )

        half = "Z" * 50_000
        text = f"{half}\f{half}"

        _lock = threading.Lock()
        _idx = [0]

        def mock_side_effect(**kwargs: object) -> LLMResponse | None:
            with _lock:
                _idx[0] += 1
                current = _idx[0]
            if current == 1:
                return LLMResponse(text=good_response, input_tokens=100, output_tokens=50)
            return None  # second chunk fails

        mock_fn = MagicMock(side_effect=mock_side_effect)
        with patch("ingestion.llm_extract.call_llm", mock_fn):
            result = extract_fields_llm(
                document_text=text,
                content_format="pdf",
                max_chars=60_000,
            )
        assert result is not None
        # Should have the result from the first chunk only
        assert result.case_count == 1
        assert result.judge_name == "Good Judge"

    def test_all_chunks_fail_returns_none(self) -> None:
        """If all chunks' LLM calls fail, return None."""
        half = "W" * 50_000
        text = f"{half}\f{half}"

        mock_fn = MagicMock(return_value=None)
        with patch("ingestion.llm_extract.call_llm", mock_fn):
            result = extract_fields_llm(
                document_text=text,
                content_format="pdf",
                max_chars=60_000,
            )
        assert result is None

    def test_chunks_processed_concurrently(self) -> None:
        """Multiple chunks should be processed concurrently via ThreadPoolExecutor."""
        # Create a 3-page document that will be split into 3 chunks.
        page = "X" * 50_000
        text = f"{page}\f{page}\f{page}"

        # Track which threads process each chunk.
        thread_ids: list[int] = []
        _lock = threading.Lock()
        # Use an Event to make LLM calls block until all threads have started,
        # proving they run concurrently rather than sequentially.
        barrier = threading.Barrier(3, timeout=5)

        response_json = json.dumps(
            {
                "judge_name": "Concurrent Judge",
                "hearing_date": "2026-03-09",
                "department": "1",
                "rulings": [
                    {
                        "case_number": None,
                        "case_title": "Test",
                        "outcome": "granted",
                        "motion_type": "msj",
                        "parties": [],
                    }
                ],
            }
        )

        def mock_concurrent_llm(**kwargs: object) -> LLMResponse:
            with _lock:
                thread_ids.append(threading.current_thread().ident)
            # All 3 threads must reach the barrier before any can proceed.
            # If execution were sequential, this would deadlock/timeout.
            barrier.wait()
            return LLMResponse(text=response_json, input_tokens=100, output_tokens=50)

        mock_fn = MagicMock(side_effect=mock_concurrent_llm)
        with patch("ingestion.llm_extract.call_llm", mock_fn):
            result = extract_fields_llm(
                document_text=text,
                content_format="pdf",
                max_chars=60_000,
            )
        assert result is not None
        # All 3 chunks should have been processed.
        assert mock_fn.call_count == 3
        # At least 2 distinct threads should have been used (concurrency).
        assert len(set(thread_ids)) >= 2

    def test_max_total_chars_skips_oversized_document(self) -> None:
        """Documents exceeding max_total_chars should return None without calling LLM."""
        large_text = "X" * 300_000  # 300K chars
        mock_fn = _mock_call_llm("{}")
        with patch("ingestion.llm_extract.call_llm", mock_fn):
            result = extract_fields_llm(
                document_text=large_text,
                content_format="pdf",
                max_total_chars=200_000,
            )
        assert result is None
        # LLM should never have been called
        assert mock_fn.call_count == 0

    def test_max_total_chars_allows_under_limit(self) -> None:
        """Documents under max_total_chars should be processed normally."""
        text = "Some ruling text " * 100  # ~1.7K chars
        response_json = json.dumps(
            {
                "judge_name": "Test Judge",
                "hearing_date": "2026-03-09",
                "department": "1",
                "rulings": [],
            }
        )
        mock_fn = _mock_call_llm(response_json)
        with patch("ingestion.llm_extract.call_llm", mock_fn):
            result = extract_fields_llm(
                document_text=text,
                content_format="pdf",
                max_total_chars=200_000,
            )
        assert result is not None
        assert mock_fn.call_count == 1

    def test_max_total_chars_none_allows_any_size(self) -> None:
        """When max_total_chars is None (default), any size document is processed."""
        large_text = "X" * 300_000
        response_json = json.dumps(
            {
                "judge_name": "Big Doc Judge",
                "hearing_date": "2026-03-09",
                "department": "1",
                "rulings": [
                    {
                        "case_number": "001",
                        "case_title": "A v. B",
                        "outcome": "granted",
                        "motion_type": "msj",
                        "parties": [],
                    }
                ],
            }
        )
        mock_fn = _mock_call_llm(response_json)
        with patch("ingestion.llm_extract.call_llm", mock_fn):
            result = extract_fields_llm(
                document_text=large_text,
                content_format="pdf",
                max_chars=80_000,
                # max_total_chars not set — defaults to None
            )
        assert result is not None
        # Should have chunked and called LLM multiple times
        assert mock_fn.call_count >= 2

    def test_max_total_chars_applies_after_html_preprocessing(self) -> None:
        """HTML preprocessing reduces size before max_total_chars check.

        A 300K HTML document might preprocess to <5K of text, which should
        pass a 200K max_total_chars limit.
        """
        # Build a large HTML doc with lots of boilerplate but small content
        boilerplate = "<style>.rule { color: red; }</style>" * 6000
        content = '<td><div id="speechSynthesis">Short ruling text</div></td>'
        html_doc = f"<html><body>{boilerplate}{content}</body></html>"
        assert len(html_doc) > 200_000  # raw HTML is large

        response_json = json.dumps(
            {
                "judge_name": "HTML Judge",
                "hearing_date": "2026-03-09",
                "department": "1",
                "rulings": [],
            }
        )
        mock_fn = _mock_call_llm(response_json)
        with patch("ingestion.llm_extract.call_llm", mock_fn):
            result = extract_fields_llm(
                document_text=html_doc,
                content_format="html",
                max_total_chars=200_000,  # limit on preprocessed text
            )
        # Preprocessed text is tiny, so it should pass the limit
        assert result is not None
        assert mock_fn.call_count == 1

    def test_large_multi_ruling_document_merges_all_chunks(self) -> None:
        """Simulate a >400K char LA multi-ruling document with 8 chunks.

        Verifies that the increased _MAX_CHUNKS (10) handles documents
        that previously exceeded the 5-chunk limit.
        """
        # Build an 8-page PDF with distinct case numbers per page
        pages: list[str] = []
        for i in range(8):
            page_text = f"Case Number: 24STCV{i:05d}\n"
            page_text += f"Smith{i} v. Jones{i}\n"
            page_text += "Ruling text content. " * 3000  # ~60K per page
            pages.append(page_text)
        text = "\f".join(pages)
        assert len(text) > 400_000  # confirms it exceeds old 5-chunk limit

        _lock = threading.Lock()
        _idx = [0]

        def mock_side_effect(**kwargs: object) -> LLMResponse:
            with _lock:
                chunk_idx = _idx[0]
                _idx[0] += 1
            # Each chunk returns one unique ruling
            response = json.dumps(
                {
                    "judge_name": "LA Multi Judge",
                    "hearing_date": "2026-03-09",
                    "department": "42",
                    "rulings": [
                        {
                            "case_number": f"24STCV{chunk_idx:05d}",
                            "case_title": f"Smith{chunk_idx} v. Jones{chunk_idx}",
                            "outcome": "granted",
                            "motion_type": "msj",
                            "parties": [],
                        }
                    ],
                }
            )
            return LLMResponse(text=response, input_tokens=100, output_tokens=50)

        mock_fn = MagicMock(side_effect=mock_side_effect)
        with patch("ingestion.llm_extract.call_llm", mock_fn):
            result = extract_fields_llm(
                document_text=text,
                content_format="pdf",
                max_chars=80_000,
            )
        assert result is not None
        # Should have processed multiple chunks (more than old limit of 5)
        assert mock_fn.call_count >= 6
        # Document-level fields from first chunk
        assert result.judge_name == "LA Multi Judge"
        assert result.department == "42"
        # Should have collected rulings from all chunks
        assert result.case_count >= 6


# ---------------------------------------------------------------------------
# max_tokens passthrough (#2355)
# ---------------------------------------------------------------------------


class TestMaxTokensPassthrough:
    """Verify extract_fields_llm passes max_tokens through to call_llm (#2355).

    Large-document counties (e.g. Santa Clara, 130K+ chars) need a higher
    max_tokens value to avoid truncated JSON responses.
    """

    def test_default_max_tokens_is_4096(self) -> None:
        """When max_tokens is not specified, call_llm receives 4096."""
        response_json = json.dumps(
            {
                "judge_name": "Test Judge",
                "hearing_date": "2026-03-09",
                "department": "1",
                "rulings": [],
            }
        )
        mock_fn = _mock_call_llm(response_json)
        with patch("ingestion.llm_extract.call_llm", mock_fn):
            extract_fields_llm(
                document_text="Some ruling text",
                content_format="pdf",
            )
        assert mock_fn.call_count == 1
        call_kwargs = mock_fn.call_args[1]
        assert call_kwargs["max_tokens"] == 4096

    def test_custom_max_tokens_passed_through(self) -> None:
        """When max_tokens is specified, call_llm receives that value."""
        response_json = json.dumps(
            {
                "judge_name": "Test Judge",
                "hearing_date": "2026-03-09",
                "department": "1",
                "rulings": [],
            }
        )
        mock_fn = _mock_call_llm(response_json)
        with patch("ingestion.llm_extract.call_llm", mock_fn):
            extract_fields_llm(
                document_text="Some ruling text",
                content_format="pdf",
                max_tokens=32768,
            )
        assert mock_fn.call_count == 1
        call_kwargs = mock_fn.call_args[1]
        assert call_kwargs["max_tokens"] == 32768

    def test_max_tokens_passed_to_all_chunks(self) -> None:
        """When document is chunked, max_tokens is passed to every chunk call."""
        page1 = "Page 1 content. " * 5000  # ~80K chars
        page2 = "Page 2 content. " * 5000  # ~80K chars
        text = f"{page1}\f{page2}"

        response_json = json.dumps(
            {
                "judge_name": "Chunk Judge",
                "hearing_date": "2026-03-09",
                "department": "1",
                "rulings": [
                    {
                        "case_number": None,
                        "case_title": "Test",
                        "outcome": "granted",
                        "motion_type": "msj",
                        "parties": [],
                    }
                ],
            }
        )

        _lock = threading.Lock()
        _calls: list[dict] = []

        def mock_side_effect(**kwargs: object) -> LLMResponse:
            with _lock:
                _calls.append(dict(kwargs))
            return LLMResponse(text=response_json, input_tokens=100, output_tokens=50)

        mock_fn = MagicMock(side_effect=mock_side_effect)
        with patch("ingestion.llm_extract.call_llm", mock_fn):
            extract_fields_llm(
                document_text=text,
                content_format="pdf",
                max_chars=80_000,
                max_tokens=32768,
            )
        # Multiple chunks should have been processed
        assert mock_fn.call_count >= 2
        # Every call should have max_tokens=32768
        for call_kwargs in _calls:
            assert call_kwargs["max_tokens"] == 32768


# ---------------------------------------------------------------------------
# Outcome taxonomy clarity (#635)
# ---------------------------------------------------------------------------


class TestOutcomeTaxonomyClarity:
    """Verify the system prompt explicitly guides the LLM on edge-case outcomes.

    Issue #635: 'denied without prejudice' was classified as 'other' because the
    taxonomy only said 'denied — motion was fully denied'.  The fix adds explicit
    guidance for 'denied without prejudice' -> 'denied' and 'granted with
    conditions' -> 'granted'.
    """

    def test_prompt_mentions_denied_without_prejudice(self) -> None:
        assert "denied without prejudice" in _SYSTEM_PROMPT

    def test_prompt_mentions_granted_with_conditions(self) -> None:
        assert "granted with conditions" in _SYSTEM_PROMPT

    def test_denied_without_prejudice_parsed_as_denied(self) -> None:
        """When the LLM correctly returns 'denied', _parse_response keeps it."""
        response_json = json.dumps(
            {
                "judge_name": "Test Judge",
                "hearing_date": "2025-12-01",
                "department": "C25",
                "rulings": [
                    {
                        "case_number": "24CV12345",
                        "case_title": "Smith v. Jones",
                        "case_type": "civil",
                        "outcome": "denied",
                        "motion_type": "motion_for_leave_to_amend",
                        "parties": [
                            {"name": "Smith", "role": "plaintiff"},
                            {"name": "Jones", "role": "defendant"},
                        ],
                    }
                ],
            }
        )
        result = _parse_response(response_json, None)
        assert result is not None
        assert result.rulings[0].outcome == "denied"

    def test_granted_with_conditions_parsed_as_granted(self) -> None:
        """When the LLM correctly returns 'granted', _parse_response keeps it."""
        response_json = json.dumps(
            {
                "judge_name": None,
                "hearing_date": None,
                "department": None,
                "rulings": [
                    {
                        "case_number": "24CV99999",
                        "case_title": "A v. B",
                        "case_type": "civil",
                        "outcome": "granted",
                        "motion_type": "preliminary_injunction",
                        "parties": [],
                    }
                ],
            }
        )
        result = _parse_response(response_json, None)
        assert result is not None
        assert result.rulings[0].outcome == "granted"


# ---------------------------------------------------------------------------
# _parse_confidence
# ---------------------------------------------------------------------------


class TestParseConfidence:
    """Tests for the _parse_confidence helper function."""

    def test_none_returns_defaults(self) -> None:
        fc = _parse_confidence(None)
        assert fc.case_number.value == "high"
        assert fc.judge.value == "high"

    def test_empty_dict_returns_defaults(self) -> None:
        fc = _parse_confidence({})
        assert fc.case_number.value == "high"

    def test_valid_confidence_scores(self) -> None:
        raw = {
            "case_number": "high",
            "case_title": "medium",
            "parties": "low",
            "judge": "high",
            "ruling_text": "high",
            "outcome": "medium",
        }
        fc = _parse_confidence(raw)
        assert fc.case_number.value == "high"
        assert fc.case_title.value == "medium"
        assert fc.parties.value == "low"
        assert fc.outcome.value == "medium"

    def test_invalid_value_defaults_to_high(self) -> None:
        raw = {"case_number": "very_high", "judge": "unknown"}
        fc = _parse_confidence(raw)
        assert fc.case_number.value == "high"
        assert fc.judge.value == "high"

    def test_non_dict_returns_defaults(self) -> None:
        fc = _parse_confidence("not a dict")  # type: ignore[arg-type]
        assert fc.case_number.value == "high"


# ---------------------------------------------------------------------------
# _parse_response with new extracted_ field names
# ---------------------------------------------------------------------------


class TestParseResponseNewFieldNames:
    """Tests for _parse_response handling of new extracted_ prefix field names."""

    def test_extracted_prefix_fields_parsed(self) -> None:
        response_json = json.dumps(
            {
                "extracted_judge_name": "Rodriguez",
                "hearing_date": "2026-03-03",
                "department": "205",
                "rulings": [
                    {
                        "extracted_case_number": "22SMCV01940",
                        "extracted_case_title": "Smith v. Jones",
                        "case_type": "civil",
                        "outcome": "granted",
                        "motion_type": "msj",
                        "extracted_parties": [
                            {"name": "Smith", "role": "plaintiff"},
                            {"name": "Jones", "role": "defendant"},
                        ],
                        "confidence": {
                            "case_number": "high",
                            "case_title": "medium",
                            "parties": "high",
                            "judge": "low",
                            "ruling_text": "high",
                            "outcome": "high",
                        },
                    }
                ],
            }
        )
        result = _parse_response(response_json, None)
        assert result is not None
        assert result.judge_name == "Rodriguez"
        assert result.rulings[0].case_number == "22SMCV01940"
        assert result.rulings[0].case_title == "Smith v. Jones"
        assert len(result.rulings[0].parties) == 2
        assert result.rulings[0].confidence.case_title.value == "medium"
        assert result.rulings[0].confidence.judge.value == "low"

    def test_old_field_names_still_work(self) -> None:
        """Backward compatibility: old field names without extracted_ prefix."""
        response_json = json.dumps(
            {
                "judge_name": "Smith",
                "hearing_date": "2026-01-01",
                "department": "1",
                "rulings": [
                    {
                        "case_number": "ABC123",
                        "case_title": "A v. B",
                        "outcome": "denied",
                        "parties": [{"name": "A", "role": "plaintiff"}],
                    }
                ],
            }
        )
        result = _parse_response(response_json, None)
        assert result is not None
        assert result.judge_name == "Smith"
        assert result.rulings[0].case_number == "ABC123"
        assert result.rulings[0].case_title == "A v. B"

    def test_confidence_defaults_when_missing(self) -> None:
        """When confidence is not in the response, defaults to all high."""
        response_json = json.dumps(
            {
                "judge_name": "Test",
                "rulings": [
                    {
                        "case_number": "123",
                        "outcome": "granted",
                        "parties": [],
                    }
                ],
            }
        )
        result = _parse_response(response_json, None)
        assert result is not None
        assert result.rulings[0].confidence.case_number.value == "high"
        assert result.rulings[0].confidence.judge.value == "high"

    def test_per_party_confidence_parsed(self) -> None:
        """Per-party confidence scores should be preserved when provided."""
        response_json = json.dumps(
            {
                "judge_name": "Test",
                "rulings": [
                    {
                        "case_number": "123",
                        "outcome": "granted",
                        "extracted_parties": [
                            {"name": "Alice", "role": "plaintiff", "confidence": "high"},
                            {"name": "Bob", "role": "defendant", "confidence": "low"},
                        ],
                    }
                ],
            }
        )
        result = _parse_response(response_json, None)
        assert result is not None
        assert len(result.rulings[0].parties) == 2
        assert result.rulings[0].parties[0]["confidence"] == "high"
        assert result.rulings[0].parties[1]["confidence"] == "low"

    def test_per_party_confidence_omitted_when_absent(self) -> None:
        """When party has no confidence field, it should not be in the dict."""
        response_json = json.dumps(
            {
                "judge_name": "Test",
                "rulings": [
                    {
                        "case_number": "123",
                        "outcome": "granted",
                        "parties": [
                            {"name": "Alice", "role": "plaintiff"},
                        ],
                    }
                ],
            }
        )
        result = _parse_response(response_json, None)
        assert result is not None
        assert "confidence" not in result.rulings[0].parties[0]

    def test_per_party_invalid_confidence_ignored(self) -> None:
        """Invalid per-party confidence values should be omitted."""
        response_json = json.dumps(
            {
                "judge_name": "Test",
                "rulings": [
                    {
                        "case_number": "123",
                        "outcome": "granted",
                        "extracted_parties": [
                            {"name": "Alice", "role": "plaintiff", "confidence": "very_sure"},
                        ],
                    }
                ],
            }
        )
        result = _parse_response(response_json, None)
        assert result is not None
        assert "confidence" not in result.rulings[0].parties[0]


# ---------------------------------------------------------------------------
# TokenTracker tests
# ---------------------------------------------------------------------------


class TestTokenTracker:
    """Tests for the TokenTracker dataclass."""

    def test_initial_values(self) -> None:
        """New tracker starts with all zeros."""
        tracker = TokenTracker()
        assert tracker.input_tokens == 0
        assert tracker.output_tokens == 0
        assert tracker.api_calls == 0

    def test_add_accumulates(self) -> None:
        """add() accumulates token counts and increments api_calls."""
        tracker = TokenTracker()
        tracker.add(100, 50)
        assert tracker.input_tokens == 100
        assert tracker.output_tokens == 50
        assert tracker.api_calls == 1

        tracker.add(200, 75)
        assert tracker.input_tokens == 300
        assert tracker.output_tokens == 125
        assert tracker.api_calls == 2

    def test_thread_safety(self) -> None:
        """Concurrent add() calls produce correct totals."""
        tracker = TokenTracker()
        num_threads = 10
        adds_per_thread = 100

        def worker() -> None:
            for _ in range(adds_per_thread):
                tracker.add(10, 5)

        threads = [threading.Thread(target=worker) for _ in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        expected_calls = num_threads * adds_per_thread
        assert tracker.api_calls == expected_calls
        assert tracker.input_tokens == expected_calls * 10
        assert tracker.output_tokens == expected_calls * 5

    def test_estimated_cost_google_default(self) -> None:
        """Default provider (google) uses Gemini Flash Lite pricing."""
        tracker = TokenTracker()
        tracker.add(1_000_000, 1_000_000)
        cost = tracker.estimated_cost()
        # Google pricing: $0.075/1M input + $0.30/1M output
        expected = 0.075 + 0.30
        assert abs(cost - expected) < 0.001

    def test_estimated_cost_anthropic(self) -> None:
        """Anthropic provider uses Haiku pricing."""
        tracker = TokenTracker()
        tracker.add(1_000_000, 1_000_000)
        cost = tracker.estimated_cost(provider="anthropic")
        # Anthropic pricing: $0.80/1M input + $4.00/1M output
        expected = 0.80 + 4.00
        assert abs(cost - expected) < 0.001

    def test_estimated_cost_custom_pricing(self) -> None:
        """Custom pricing overrides provider defaults."""
        tracker = TokenTracker()
        tracker.add(1_000_000, 1_000_000)
        cost = tracker.estimated_cost(
            input_price_per_mtok=1.0,
            output_price_per_mtok=2.0,
        )
        assert abs(cost - 3.0) < 0.001

    def test_estimated_cost_zero_tokens(self) -> None:
        """Zero tokens produces zero cost."""
        tracker = TokenTracker()
        assert tracker.estimated_cost() == 0.0

    def test_estimated_cost_unknown_provider_defaults_to_google(self) -> None:
        """Unknown provider falls back to google pricing."""
        tracker = TokenTracker()
        tracker.add(1_000_000, 1_000_000)
        cost_unknown = tracker.estimated_cost(provider="unknown_provider")
        cost_google = tracker.estimated_cost(provider="google")
        assert abs(cost_unknown - cost_google) < 0.001


class TestTokenTrackerIntegration:
    """Test that token_tracker is threaded through extract_fields_llm."""

    def test_single_chunk_tracks_tokens(self) -> None:
        """Token tracker accumulates tokens from a single-chunk extraction."""
        response_json = json.dumps(
            {
                "judge_name": "Test Judge",
                "rulings": [
                    {
                        "case_number": "123",
                        "case_title": "A v. B",
                        "outcome": "granted",
                    }
                ],
            }
        )
        mock_fn = MagicMock(
            return_value=LLMResponse(
                text=response_json,
                input_tokens=500,
                output_tokens=200,
            )
        )
        tracker = TokenTracker()
        with patch("ingestion.llm_extract.call_llm", mock_fn):
            result = extract_fields_llm(
                document_text="Short ruling text.",
                content_format="pdf",
                token_tracker=tracker,
            )
        assert result is not None
        assert tracker.input_tokens == 500
        assert tracker.output_tokens == 200
        assert tracker.api_calls == 1

    def test_multi_chunk_tracks_tokens(self) -> None:
        """Token tracker accumulates tokens across multiple chunks."""
        response_json = json.dumps(
            {
                "judge_name": "Test Judge",
                "rulings": [
                    {
                        "case_number": "123",
                        "case_title": "A v. B",
                        "outcome": "granted",
                    }
                ],
            }
        )
        call_count = 0

        def mock_call_llm(**kwargs: object) -> LLMResponse:
            nonlocal call_count
            call_count += 1
            return LLMResponse(
                text=response_json,
                input_tokens=300,
                output_tokens=100,
            )

        tracker = TokenTracker()
        # Create text large enough to be chunked (>80K chars)
        long_text = "A" * 90_000 + "\n\n" + "B" * 90_000
        with patch("ingestion.llm_extract.call_llm", side_effect=mock_call_llm):
            result = extract_fields_llm(
                document_text=long_text,
                content_format="pdf",
                max_chars=80_000,
                token_tracker=tracker,
            )
        assert result is not None
        assert call_count >= 2
        assert tracker.api_calls == call_count
        assert tracker.input_tokens == 300 * call_count
        assert tracker.output_tokens == 100 * call_count

    def test_no_tracker_does_not_error(self) -> None:
        """Passing no token_tracker (default) does not raise errors."""
        response_json = json.dumps(
            {
                "judge_name": "Test Judge",
                "rulings": [
                    {
                        "case_number": "123",
                        "outcome": "granted",
                    }
                ],
            }
        )
        mock_fn = MagicMock(
            return_value=LLMResponse(
                text=response_json,
                input_tokens=100,
                output_tokens=50,
            )
        )
        with patch("ingestion.llm_extract.call_llm", mock_fn):
            result = extract_fields_llm(
                document_text="Some text.",
                content_format="pdf",
            )
        assert result is not None

    def test_failed_llm_call_does_not_track(self) -> None:
        """When LLM call returns None, no tokens are tracked."""
        tracker = TokenTracker()
        with patch("ingestion.llm_extract.call_llm", return_value=None):
            result = extract_fields_llm(
                document_text="Some text.",
                content_format="pdf",
                token_tracker=tracker,
            )
        assert result is None
        assert tracker.input_tokens == 0
        assert tracker.output_tokens == 0
        assert tracker.api_calls == 0


# ---------------------------------------------------------------------------
# _serialize_result / _deserialize_result round-trip tests (#2217)
# ---------------------------------------------------------------------------


class TestSerializeDeserializeRoundTrip:
    """Round-trip tests for cache serialization of LLMExtractionResult."""

    def test_roundtrip_minimal_result(self) -> None:
        """Minimal result with no rulings round-trips correctly."""
        result = LLMExtractionResult()
        deserialized = _deserialize_result(_serialize_result(result))
        assert deserialized is not None
        assert deserialized.judge_name is None
        assert deserialized.hearing_date is None
        assert deserialized.department is None
        assert deserialized.case_count == 0
        assert deserialized.rulings == []

    def test_roundtrip_with_hearing_date(self) -> None:
        """Result with hearing_date round-trips correctly (date <-> str)."""
        result = LLMExtractionResult(
            judge_name="Smith",
            hearing_date=date(2025, 6, 15),
            department="C10",
            case_count=1,
        )
        deserialized = _deserialize_result(_serialize_result(result))
        assert deserialized is not None
        assert deserialized.judge_name == "Smith"
        assert deserialized.hearing_date == date(2025, 6, 15)
        assert deserialized.department == "C10"
        assert deserialized.case_count == 1

    def test_roundtrip_with_default_confidence(self) -> None:
        """Result with rulings using default FieldConfidence round-trips."""
        ruling = LLMRulingResult(
            case_number="23CV001",
            case_title="Doe v. Roe",
            outcome="granted",
        )
        result = LLMExtractionResult(rulings=[ruling], case_count=1)
        deserialized = _deserialize_result(_serialize_result(result))
        assert deserialized is not None
        assert len(deserialized.rulings) == 1
        r = deserialized.rulings[0]
        assert r.case_number == "23CV001"
        assert r.case_title == "Doe v. Roe"
        assert r.outcome == "granted"
        assert isinstance(r.confidence, FieldConfidence)
        assert r.confidence.case_number == ConfidenceLevel.HIGH
        assert r.confidence.outcome == ConfidenceLevel.HIGH

    def test_roundtrip_with_non_default_confidence(self) -> None:
        """Result with non-default confidence levels round-trips correctly."""
        confidence = FieldConfidence(
            case_number=ConfidenceLevel.LOW,
            case_title=ConfidenceLevel.MEDIUM,
            parties=ConfidenceLevel.LOW,
            judge=ConfidenceLevel.HIGH,
            ruling_text=ConfidenceLevel.MEDIUM,
            outcome=ConfidenceLevel.LOW,
        )
        ruling = LLMRulingResult(
            case_number="24FAM100",
            confidence=confidence,
        )
        result = LLMExtractionResult(rulings=[ruling], case_count=1)
        deserialized = _deserialize_result(_serialize_result(result))
        assert deserialized is not None
        r = deserialized.rulings[0]
        assert r.confidence.case_number == ConfidenceLevel.LOW
        assert r.confidence.case_title == ConfidenceLevel.MEDIUM
        assert r.confidence.parties == ConfidenceLevel.LOW
        assert r.confidence.judge == ConfidenceLevel.HIGH
        assert r.confidence.ruling_text == ConfidenceLevel.MEDIUM
        assert r.confidence.outcome == ConfidenceLevel.LOW

    def test_roundtrip_with_parties(self) -> None:
        """Result with parties round-trips correctly."""
        ruling = LLMRulingResult(
            case_number="23CV001",
            parties=[
                {"name": "Alice", "role": "plaintiff"},
                {"name": "Bob", "role": "defendant"},
            ],
        )
        result = LLMExtractionResult(rulings=[ruling], case_count=1)
        deserialized = _deserialize_result(_serialize_result(result))
        assert deserialized is not None
        r = deserialized.rulings[0]
        assert len(r.parties) == 2
        assert r.parties[0] == {"name": "Alice", "role": "plaintiff"}
        assert r.parties[1] == {"name": "Bob", "role": "defendant"}

    def test_roundtrip_multiple_rulings(self) -> None:
        """Result with multiple rulings round-trips correctly."""
        rulings = [
            LLMRulingResult(case_number="23CV001", outcome="granted"),
            LLMRulingResult(
                case_number="23CV002",
                outcome="denied",
                confidence=FieldConfidence(outcome=ConfidenceLevel.MEDIUM),
            ),
        ]
        result = LLMExtractionResult(
            judge_name="Jones",
            hearing_date=date(2025, 1, 10),
            rulings=rulings,
            case_count=2,
        )
        deserialized = _deserialize_result(_serialize_result(result))
        assert deserialized is not None
        assert len(deserialized.rulings) == 2
        assert deserialized.rulings[0].case_number == "23CV001"
        assert deserialized.rulings[1].case_number == "23CV002"
        assert deserialized.rulings[1].confidence.outcome == ConfidenceLevel.MEDIUM

    def test_roundtrip_empty_rulings_list(self) -> None:
        """Result with explicit empty rulings list round-trips correctly."""
        result = LLMExtractionResult(
            judge_name="Brown",
            rulings=[],
            case_count=0,
        )
        deserialized = _deserialize_result(_serialize_result(result))
        assert deserialized is not None
        assert deserialized.rulings == []
        assert deserialized.judge_name == "Brown"

    def test_deserialize_empty_list_returns_none(self) -> None:
        """Deserializing an empty list returns None."""
        assert _deserialize_result([]) is None

    def test_serialize_result_is_json_serializable(self) -> None:
        """Serialized result can be passed to json.dumps without error.

        This is the actual failure mode of the bug -- FieldConfidence
        objects are not JSON-serializable, causing cache.put() to fail.
        """
        ruling = LLMRulingResult(
            case_number="23CV001",
            case_title="Doe v. Roe",
            confidence=FieldConfidence(
                case_number=ConfidenceLevel.LOW,
                outcome=ConfidenceLevel.MEDIUM,
            ),
            parties=[{"name": "Doe", "role": "plaintiff"}],
        )
        result = LLMExtractionResult(
            judge_name="Smith",
            hearing_date=date(2025, 3, 1),
            department="C10",
            case_count=1,
            rulings=[ruling],
        )
        serialized = _serialize_result(result)
        # This must not raise TypeError
        json_str = json.dumps(serialized, indent=2)
        # Verify it can be parsed back
        parsed = json.loads(json_str)
        assert isinstance(parsed, list)
        assert len(parsed) == 1

    def test_cache_put_succeeds_with_serialized_result(self) -> None:
        """Cache put() succeeds when given serialized result (no TypeError).

        Simulates the cache write path in extract_fields_llm().
        """
        ruling = LLMRulingResult(
            case_number="23CV001",
            confidence=FieldConfidence(outcome=ConfidenceLevel.LOW),
        )
        result = LLMExtractionResult(rulings=[ruling], case_count=1)
        serialized = _serialize_result(result)

        # Simulate what _LlmCache.put() does
        body = json.dumps(serialized, indent=2).encode()
        assert isinstance(body, bytes)
        # Verify it round-trips through JSON
        loaded = json.loads(body)
        assert loaded[0]["rulings"][0]["confidence"]["outcome"] == "low"


# ---------------------------------------------------------------------------
# #2424: --bust-llm-cache flag plumbing
# ---------------------------------------------------------------------------


class TestExtractFieldsLlmBustCache:
    """Verify bust_cache=True skips cache read but preserves cache write."""

    @patch("ingestion.llm_extract._get_llm_cache")
    @patch("ingestion.llm_extract.call_llm")
    def test_bust_cache_skips_cache_get_calls_llm(
        self,
        mock_call_llm: MagicMock,
        mock_get_cache: MagicMock,
    ) -> None:
        """With bust_cache=True, cache.get is NOT called and the LLM IS."""
        cache = MagicMock()
        cache.get.return_value = {
            "rulings": [],
            "case_count": 0,
        }
        mock_get_cache.return_value = cache
        mock_call_llm.return_value = LLMResponse(
            text='{"rulings": [{"case_number": "23CV001"}]}',
            input_tokens=10,
            output_tokens=5,
        )

        client = MagicMock()
        result = extract_fields_llm(
            document_text="Case No. 23CV001. Motion granted.",
            content_format="html",
            client=client,
            provider="google",
            model="gemini-2.5-flash-lite",
            bust_cache=True,
        )

        cache.get.assert_not_called()
        mock_call_llm.assert_called()  # LLM was actually called.
        assert result is not None

    @patch("ingestion.llm_extract._get_llm_cache")
    @patch("ingestion.llm_extract.call_llm")
    def test_cache_hit_honored_without_bust_cache(
        self,
        mock_call_llm: MagicMock,
        mock_get_cache: MagicMock,
    ) -> None:
        """With bust_cache=False (default), cache.get short-circuits; the
        LLM is NOT invoked."""
        cache = MagicMock()
        cache.get.return_value = [
            {
                "rulings": [{"case_number": "CACHED-123"}],
                "case_count": 1,
            }
        ]
        mock_get_cache.return_value = cache

        client = MagicMock()
        result = extract_fields_llm(
            document_text="irrelevant",
            content_format="html",
            client=client,
            provider="google",
            model="gemini-2.5-flash-lite",
            bust_cache=False,
        )

        cache.get.assert_called_once()
        mock_call_llm.assert_not_called()
        assert result is not None
