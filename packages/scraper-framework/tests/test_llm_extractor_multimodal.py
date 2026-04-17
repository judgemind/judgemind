"""Tests for multimodal extraction in LlmExtractor (#1589, #1590).

Validates:
1. LlmExtractor accepts provider parameter ("anthropic" or "google").
2. extract_from_pdf() renders pages and sends ONE image per LLM call.
3. Per-page row parsing (_parse_page_rows).
4. Join logic (_is_new_case, _join_page_rows) for cross-page continuations.
5. Existing extract(text) path remains unchanged.
6. Error handling (empty PDF, render failures, API failures).
7. _render_pdf_pages helper.
8. _create_google_client helper.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import anthropic
import pytest

from framework.llm_extractor import (
    LlmExtractor,
    _create_google_client,
    _deduplicate_ruling_texts,
    _drop_calendar_listing_rulings,
    _extract_case_number_from_info,
    _extract_case_title_from_info,
    _is_calendar_header,
    _is_calendar_listing_only,
    _is_new_case,
    _join_page_rows,
    _parse_page_rows,
    _render_pdf_pages,
    _resolve_cross_references,
)
from framework.llm_schema import ExtractedRuling

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SAMPLE_PDF_PATH = FIXTURES_DIR / "sample_ruling.pdf"

# Per-page row JSON responses (new per-page format).
SINGLE_PAGE_ROWS_JSON = json.dumps(
    [
        {
            "entry_number": 1,
            "case_info": "2024-01393434 Martinez v. ABC Manufacturing Inc.",
            "ruling_text": "The motion for summary adjudication is DENIED.",
        }
    ]
)

MULTI_CASE_PAGE_ROWS_JSON = json.dumps(
    [
        {
            "entry_number": 1,
            "case_info": "2024-01393434 Martinez v. ABC Manufacturing Inc.",
            "ruling_text": "The motion for summary adjudication is DENIED.",
        },
        {
            "entry_number": 2,
            "case_info": "2024-00567890 Garcia v. State Farm Insurance",
            "ruling_text": "The motion is GRANTED.",
        },
    ]
)

# Continuation rows (no entry number, partial text from a case that spans pages).
CONTINUATION_PAGE_ROWS_JSON = json.dumps(
    [
        {
            "entry_number": None,
            "case_info": "",
            "ruling_text": "...the court finds that the motion lacks merit.",
        },
        {
            "entry_number": 3,
            "case_info": "2024-00999999 Thompson v. City of Palm Springs",
            "ruling_text": "The demurrer is OVERRULED.",
        },
    ]
)

# Legacy full-document JSON (used for backward compat tests).
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
# _parse_page_rows tests
# ---------------------------------------------------------------------------


class TestParsePageRows:
    """Tests for the _parse_page_rows helper."""

    def test_parses_valid_json_array(self) -> None:
        """Parses a valid JSON array of row objects."""
        rows = _parse_page_rows(SINGLE_PAGE_ROWS_JSON, page_index=0)
        assert len(rows) == 1
        assert rows[0]["entry_number"] == 1
        assert "Martinez" in rows[0]["case_info"]
        assert "DENIED" in rows[0]["ruling_text"]

    def test_parses_multi_row_array(self) -> None:
        """Parses multiple rows from a single page."""
        rows = _parse_page_rows(MULTI_CASE_PAGE_ROWS_JSON, page_index=0)
        assert len(rows) == 2
        assert rows[0]["entry_number"] == 1
        assert rows[1]["entry_number"] == 2

    def test_handles_null_entry_number(self) -> None:
        """Null entry_number is preserved."""
        rows = _parse_page_rows(CONTINUATION_PAGE_ROWS_JSON, page_index=0)
        assert rows[0]["entry_number"] is None
        assert rows[1]["entry_number"] == 3

    def test_strips_trailing_period_from_entry_number(self) -> None:
        """Entry numbers with trailing periods are normalized."""
        raw = json.dumps([{"entry_number": "5.", "case_info": "test", "ruling_text": "text"}])
        rows = _parse_page_rows(raw, page_index=0)
        assert rows[0]["entry_number"] == 5

    def test_handles_string_entry_number(self) -> None:
        """String entry numbers are converted to int."""
        raw = json.dumps([{"entry_number": "12", "case_info": "test", "ruling_text": "text"}])
        rows = _parse_page_rows(raw, page_index=0)
        assert rows[0]["entry_number"] == 12

    def test_handles_invalid_entry_number(self) -> None:
        """Non-numeric entry numbers become None."""
        raw = json.dumps([{"entry_number": "abc", "case_info": "test", "ruling_text": "text"}])
        rows = _parse_page_rows(raw, page_index=0)
        assert rows[0]["entry_number"] is None

    def test_handles_empty_array(self) -> None:
        """Empty array returns empty list."""
        rows = _parse_page_rows("[]", page_index=0)
        assert rows == []

    def test_handles_markdown_code_fences(self) -> None:
        """Strips markdown code fences from response."""
        raw = "```json\n" + SINGLE_PAGE_ROWS_JSON + "\n```"
        rows = _parse_page_rows(raw, page_index=0)
        assert len(rows) == 1

    def test_handles_dict_with_rows_key(self) -> None:
        """Handles dict response with 'rows' key."""
        raw = json.dumps({"rows": [{"entry_number": 1, "case_info": "test", "ruling_text": "t"}]})
        rows = _parse_page_rows(raw, page_index=0)
        assert len(rows) == 1

    def test_handles_dict_with_rulings_key(self) -> None:
        """Handles dict response with 'rulings' key (visual-structure prompt format)."""
        raw = json.dumps(
            {
                "rulings": [
                    {"entry_number": "101", "case_info": "Smith vs Jones", "ruling_text": "t"},
                    {"entry_number": "", "case_info": "", "ruling_text": "continuation"},
                ]
            }
        )
        rows = _parse_page_rows(raw, page_index=0)
        assert len(rows) == 2
        assert rows[0]["entry_number"] == 101
        assert rows[1]["entry_number"] is None

    def test_handles_invalid_json(self) -> None:
        """Invalid JSON returns empty list."""
        rows = _parse_page_rows("not json at all", page_index=0)
        assert rows == []

    def test_filters_non_dict_entries(self) -> None:
        """Non-dict entries in the array are filtered out."""
        raw = json.dumps(
            [
                {"entry_number": 1, "case_info": "test", "ruling_text": "t"},
                "not a dict",
                42,
                None,
            ]
        )
        rows = _parse_page_rows(raw, page_index=0)
        assert len(rows) == 1

    def test_missing_case_info_defaults_empty(self) -> None:
        """Missing case_info defaults to empty string."""
        raw = json.dumps([{"entry_number": 1, "ruling_text": "text"}])
        rows = _parse_page_rows(raw, page_index=0)
        assert rows[0]["case_info"] == ""

    def test_invalid_json_with_brackets_but_bad_content(self) -> None:
        """JSON with brackets but invalid content inside returns empty list."""
        raw = "Some preamble [invalid json content] trailing text"
        rows = _parse_page_rows(raw, page_index=0)
        assert rows == []

    def test_parsed_number_returns_empty(self) -> None:
        """If parsed JSON is a bare number, returns empty list."""
        rows = _parse_page_rows("42", page_index=0)
        assert rows == []

    def test_parsed_string_returns_empty(self) -> None:
        """If parsed JSON is a bare string, returns empty list."""
        rows = _parse_page_rows('"just a string"', page_index=0)
        assert rows == []

    # --- New 4-field format tests ---

    def test_new_format_case_number_and_case_title(self) -> None:
        """New 4-field format with separate case_number and case_title."""
        raw = json.dumps(
            {
                "page_header": None,
                "rulings": [
                    {
                        "entry_number": "1",
                        "case_number": "C22-01971",
                        "case_title": "Marquez vs. Kohl's Department Stores, Inc.",
                        "ruling_text": "The motion is denied as moot.",
                    }
                ],
            }
        )
        rows = _parse_page_rows(raw, page_index=0)
        assert len(rows) == 1
        assert rows[0]["entry_number"] == 1
        assert "C22-01971" in rows[0]["case_info"]
        assert "Marquez" in rows[0]["case_info"]
        assert "denied" in rows[0]["ruling_text"]

    def test_new_format_null_case_number(self) -> None:
        """Null case_number should not produce 'None' in case_info."""
        raw = json.dumps(
            {
                "page_header": None,
                "rulings": [
                    {
                        "entry_number": "1",
                        "case_number": None,
                        "case_title": "Smith vs. Jones",
                        "ruling_text": "Granted.",
                    }
                ],
            }
        )
        rows = _parse_page_rows(raw, page_index=0)
        assert "None" not in rows[0]["case_info"]
        assert "Smith vs. Jones" in rows[0]["case_info"]

    def test_page_header_emits_synthetic_row(self) -> None:
        """page_header produces a synthetic header row for _join_page_rows."""
        raw = json.dumps(
            {
                "page_header": {
                    "department": "16",
                    "judge_name": "Benjamin T Reyes II",
                    "hearing_date": "2026-03-25",
                },
                "rulings": [],
            }
        )
        rows = _parse_page_rows(raw, page_index=0)
        assert len(rows) == 1
        assert rows[0]["entry_number"] is None
        assert rows[0]["ruling_text"] == ""
        assert "Department 16" in rows[0]["case_info"]
        assert "JUDGE Benjamin T Reyes II" in rows[0]["case_info"]
        assert "Hearing Date: 2026-03-25" in rows[0]["case_info"]

    def test_line_number_entry_number(self) -> None:
        """'Line 2' style entry numbers should extract the numeric portion."""
        raw = json.dumps(
            [{"entry_number": "Line 2", "case_info": "test v. test", "ruling_text": "t"}]
        )
        rows = _parse_page_rows(raw, page_index=0)
        assert rows[0]["entry_number"] == 2

    def test_parenthesized_entry_number(self) -> None:
        """'(47)' style entry numbers should be parsed."""
        raw = json.dumps(
            [{"entry_number": "(47)", "case_info": "test v. test", "ruling_text": "t"}]
        )
        rows = _parse_page_rows(raw, page_index=0)
        assert rows[0]["entry_number"] == 47

    def test_trailing_county_prefix_stripped(self) -> None:
        """Trailing single-letter county prefix is stripped from case_title."""
        raw = json.dumps(
            {
                "rulings": [
                    {
                        "entry_number": "1",
                        "case_number": "22-01971",
                        "case_title": "SMITH VS. JONES C",
                        "ruling_text": "Denied.",
                    }
                ]
            }
        )
        rows = _parse_page_rows(raw, page_index=0)
        assert not rows[0]["case_info"].endswith(" C")

    # ---------------------------------------------------------------------------
    # _CASE_NUMBER_RE tests
    # ---------------------------------------------------------------------------

    def test_json_object_in_text(self) -> None:
        """JSON object embedded in non-JSON text is extracted."""
        raw = (
            "Here is the result: "
            '{"rulings": [{"entry_number": "1",'
            ' "case_info": "test v. case",'
            ' "ruling_text": "t"}]} end'
        )
        rows = _parse_page_rows(raw, page_index=0)
        assert len(rows) == 1
        assert rows[0]["entry_number"] == 1

    def test_json_object_bad_content(self) -> None:
        """JSON object with invalid content returns empty."""
        raw = "Some text {not valid json} more text"
        rows = _parse_page_rows(raw, page_index=0)
        assert rows == []


class TestExtractCaseTitleTruncation:
    """Tests for case_title truncation in _extract_case_title_from_info."""

    def test_long_title_with_vs_and_case_number(self) -> None:
        """Titles with v. followed by a second case number are truncated."""
        info = "Smith v. Jones 26CV484550 David Rome v. Other Party 23CV417411"
        title = _extract_case_title_from_info(info)
        assert title is not None
        assert len(title) < 150
        assert "Smith v. Jones" in title

    def test_probate_title_with_multiple_cases(self) -> None:
        """Probate titles with multiple CONSERVATORSHIP/ESTATE patterns are truncated."""
        info = (
            "IN THE MATTER OF: CHARLENE DOWNS "
            "CONSERVATORSHIP OF KENNETH CARLSON MSP12-00793 "
            "ESTATE OF HENRY FASQUELLE GUARDIANSHIP OF NAYELLI BRONSON"
        )
        title = _extract_case_title_from_info(info)
        assert title is not None
        assert len(title) <= 150

    def test_hard_truncation_fallback(self) -> None:
        """Titles over 150 chars without v. or probate pattern are hard-truncated."""
        info = "A " * 100  # 200 chars, no v. or probate pattern
        title = _extract_case_title_from_info(info)
        assert title is not None
        assert len(title) <= 150

    def test_trailing_county_prefix_stripped(self) -> None:
        """Trailing C or N from case number prefix is stripped."""
        info = "SMITH VS. JONES C\n22-01971"
        title = _extract_case_title_from_info(info)
        assert title is not None
        assert not title.endswith(" C")

    def test_none_artifact_removed(self) -> None:
        """'None' text artifacts from null JSON fields are removed."""
        info = "None None SMITH VS. JONES\n24-378499"
        title = _extract_case_title_from_info(info)
        assert title is not None
        assert "None" not in title


class TestJoinPageRowsHeaderDate:
    """Tests for hearing_date extraction from page_header rows."""

    def test_hearing_date_from_header(self) -> None:
        """hearing_date is extracted from synthetic header rows."""
        rows = [
            {
                "entry_number": None,
                "case_info": "Department 16\nJUDGE Test\nHearing Date: 2026-03-25",
                "ruling_text": "",
            },
            {
                "entry_number": 1,
                "case_info": "Smith v. Jones\nC22-01971",
                "ruling_text": "Granted.",
            },
        ]
        rulings = _join_page_rows(rows)
        assert len(rulings) == 1
        assert rulings[0].hearing_date == "2026-03-25"
        assert rulings[0].extracted_judge_name == "Test"
        assert rulings[0].department == "16"


class TestCaseNumberRegex:
    """Tests for the expanded _CASE_NUMBER_RE pattern."""

    def test_oc_dash_format(self) -> None:
        assert _extract_case_number_from_info("2024-01393434 Martinez") is not None

    def test_oc_family_format(self) -> None:
        assert _extract_case_number_from_info("25D006297 BAEZ V. BAEZ") is not None

    def test_riverside_format(self) -> None:
        assert _extract_case_number_from_info("CVRI2401570 SMITH VS DOE") is not None

    def test_santa_clara_format(self) -> None:
        assert _extract_case_number_from_info("26CV484550 City v. Feil") is not None

    def test_fresno_format(self) -> None:
        assert _extract_case_number_from_info("18CECG00898 Alarcon v. Monroe") is not None

    def test_sf_format(self) -> None:
        assert _extract_case_number_from_info("FPT-24-378499 Fuller v. Robinson") is not None

    def test_cc_format(self) -> None:
        assert _extract_case_number_from_info("C22-01971 Marquez v. Kohl's") is not None

    def test_sb_format(self) -> None:
        assert _extract_case_number_from_info("CIVSB2116995 Muro v. Safety") is not None

    def test_ventura_format(self) -> None:
        assert _extract_case_number_from_info("2024CUOR027466 Williams v. Paseo") is not None

    def test_no_false_positive_on_word(self) -> None:
        assert _extract_case_number_from_info("HEARING ON MOTION") is None


# ---------------------------------------------------------------------------
# _is_new_case tests
# ---------------------------------------------------------------------------


class TestIsNewCase:
    """Tests for the _is_new_case helper."""

    def test_valid_entry_with_case_number(self) -> None:
        """Entry with valid number and case number pattern is new case."""
        row = {"entry_number": 1, "case_info": "2024-01393434 Smith v. Jones", "ruling_text": "t"}
        assert _is_new_case(row) is True

    def test_valid_entry_with_vs_pattern(self) -> None:
        """Entry with valid number and vs pattern is new case."""
        row = {"entry_number": 1, "case_info": "Smith v. Jones", "ruling_text": "t"}
        assert _is_new_case(row) is True

    def test_valid_entry_with_vs_dot_pattern(self) -> None:
        """Entry with 'vs.' pattern is new case."""
        row = {"entry_number": 2, "case_info": "Smith vs. Jones", "ruling_text": "t"}
        assert _is_new_case(row) is True

    def test_null_entry_number_is_continuation(self) -> None:
        """Null entry_number means continuation."""
        row = {"entry_number": None, "case_info": "2024-01393434", "ruling_text": "t"}
        assert _is_new_case(row) is False

    def test_entry_without_case_info_is_continuation(self) -> None:
        """Entry with number but no case info is continuation."""
        row = {"entry_number": 1, "case_info": "", "ruling_text": "t"}
        assert _is_new_case(row) is False

    def test_entry_with_only_text_is_continuation(self) -> None:
        """Entry with number but only text (no case number, no vs) is continuation."""
        row = {"entry_number": 1, "case_info": "some random text", "ruling_text": "t"}
        assert _is_new_case(row) is False

    def test_seven_digit_case_number(self) -> None:
        """Seven-digit case number pattern is recognized."""
        row = {"entry_number": 1, "case_info": "0012345 Smith v. Jones", "ruling_text": "t"}
        assert _is_new_case(row) is True

    def test_eight_digit_case_number(self) -> None:
        """Eight-digit case number pattern is recognized."""
        row = {"entry_number": 1, "case_info": "30012345 Smith v. Jones", "ruling_text": "t"}
        assert _is_new_case(row) is True


# ---------------------------------------------------------------------------
# _extract_case_number_from_info tests
# ---------------------------------------------------------------------------


class TestExtractCaseNumberFromInfo:
    """Tests for case number extraction from case_info."""

    def test_extracts_oc_format(self) -> None:
        """Extracts OC-style case number."""
        result = _extract_case_number_from_info("2024-01393434 Smith v. Jones")
        assert result == "2024-01393434"

    def test_extracts_seven_digit(self) -> None:
        """Extracts seven-digit case number."""
        result = _extract_case_number_from_info("0012345 Smith v. Jones")
        assert result == "0012345"

    def test_no_case_number(self) -> None:
        """Returns None when no case number is found."""
        result = _extract_case_number_from_info("Smith v. Jones only")
        assert result is None

    def test_strips_county_prefix(self) -> None:
        """County prefix is stripped from case number."""
        result = _extract_case_number_from_info("30-2024-01393434 Smith v. Jones")
        assert result == "2024-01393434"


# ---------------------------------------------------------------------------
# _extract_case_title_from_info tests
# ---------------------------------------------------------------------------


class TestExtractCaseTitleFromInfo:
    """Tests for case title extraction from case_info."""

    def test_extracts_title_after_number(self) -> None:
        """Extracts title after case number."""
        result = _extract_case_title_from_info("2024-01393434 Smith v. Jones")
        assert result == "Smith v. Jones"

    def test_full_case_info(self) -> None:
        """Extracts title from full case_info string."""
        result = _extract_case_title_from_info("Smith v. Jones")
        assert result == "Smith v. Jones"

    def test_empty_after_number(self) -> None:
        """Returns None when only number is present."""
        result = _extract_case_title_from_info("2024-01393434")
        assert result is None

    def test_newlines_replaced_with_spaces(self) -> None:
        """Embedded newlines in case_info are collapsed to spaces."""
        result = _extract_case_title_from_info("Bevli vs.\nFirestone")
        assert result == "Bevli vs. Firestone"

    def test_case_number_fragments_removed(self) -> None:
        """OC-style case number fragments are stripped from the title."""
        result = _extract_case_title_from_info("Bevli vs.\nFirestone\n30-2024-\n-CU-\nOR-CJC")
        assert result == "Bevli vs. Firestone"

    def test_court_name_fragments_removed(self) -> None:
        """Court name fragments are stripped from the title."""
        result = _extract_case_title_from_info(
            "Superior Court of the State of California\nSmith v. Jones"
        )
        assert result == "Smith v. Jones"

    def test_county_name_fragments_removed(self) -> None:
        """County name fragments are stripped from the title."""
        result = _extract_case_title_from_info("County of Orange\nSmith v. Jones")
        assert result == "Smith v. Jones"

    def test_mixed_fragments_full_cleanup(self) -> None:
        """Full cleanup of a real-world messy case_info string."""
        result = _extract_case_title_from_info(
            "30-2024-01393434\nBevli vs.\nFirestone\n30-2024-\n-CU-\nOR-CJC"
        )
        assert result == "Bevli vs. Firestone"

    def test_case_number_suffix_fragments(self) -> None:
        """Various OC case number suffix patterns are stripped."""
        # -CL- (limited civil), -PR- (probate), -FL- (family law)
        result = _extract_case_title_from_info("Smith v. Jones\n-CL-\nOR-CJC")
        assert result == "Smith v. Jones"

    def test_only_fragments_returns_none(self) -> None:
        """Returns None when case_info contains only case number fragments."""
        result = _extract_case_title_from_info("30-2024-\n-CU-\nOR-CJC")
        assert result is None

    def test_whitespace_collapsed(self) -> None:
        """Multiple spaces from cleanup are collapsed to single space."""
        result = _extract_case_title_from_info("Smith  v.   Jones")
        assert result == "Smith v. Jones"

    def test_dual_vs_contamination_truncated_short(self) -> None:
        """Dual-"vs." contamination is truncated at the first pair (#2369).

        When the multimodal LLM's JSON response leaks an adjacent case into
        ``case_info``, the result contains TWO "vs." clauses.  The cleanup
        keeps only the first plaintiff-vs-defendant pair.
        """
        contaminated = (
            "Anh-Vu Nguyen vs. Freedom Medical Group, LLC Anh-Vu Nguyen vs. Seed4Planet, LLC"
        )
        result = _extract_case_title_from_info(contaminated)
        assert result is not None
        # Only the first plaintiff-vs-defendant pair survives.
        assert result == "Anh-Vu Nguyen vs. Freedom Medical Group, LLC"
        # Exactly one "vs." or "v." remains in the cleaned title.
        vs_count = len(re.findall(r"\bv(?:s)?\.?\s", result, re.IGNORECASE))
        assert vs_count == 1

    def test_dual_vs_contamination_truncated_v_variant(self) -> None:
        """Dual-"v." pattern is also detected and truncated (#2369).

        The common OC contamination pattern repeats the first plaintiff's
        name when adjacent cases bleed together.  The cut point is the
        whitespace before the second occurrence of the plaintiff's first
        word.
        """
        contaminated = "Smith v. Jones Smith v. Roberts"
        result = _extract_case_title_from_info(contaminated)
        assert result is not None
        assert result == "Smith v. Jones"

    def test_single_vs_pattern_preserved(self) -> None:
        """A single "vs." pair is left untouched (regression for #2369)."""
        result = _extract_case_title_from_info("Anh-Vu Nguyen vs. Freedom Medical Group, LLC")
        assert result == "Anh-Vu Nguyen vs. Freedom Medical Group, LLC"


# ---------------------------------------------------------------------------
# _join_page_rows tests
# ---------------------------------------------------------------------------


class TestJoinPageRows:
    """Tests for the _join_page_rows function."""

    def test_single_case(self) -> None:
        """Single case produces one ExtractedRuling."""
        rows = [
            {
                "entry_number": 1,
                "case_info": "2024-01393434 Smith v. Jones",
                "ruling_text": "GRANTED.",
            },
        ]
        rulings = _join_page_rows(rows)
        assert len(rulings) == 1
        assert rulings[0].extracted_case_number == "2024-01393434"
        assert rulings[0].extracted_case_title == "Smith v. Jones"
        assert rulings[0].ruling_text == "GRANTED."

    def test_multiple_cases(self) -> None:
        """Multiple cases produce multiple ExtractedRulings."""
        rows = [
            {"entry_number": 1, "case_info": "2024-00001 Alpha v. Beta", "ruling_text": "GRANTED."},
            {"entry_number": 2, "case_info": "2024-00002 Gamma v. Delta", "ruling_text": "DENIED."},
        ]
        rulings = _join_page_rows(rows)
        assert len(rulings) == 2
        assert rulings[0].extracted_case_number == "2024-00001"
        assert rulings[1].extracted_case_number == "2024-00002"

    def test_continuation_merges_into_previous(self) -> None:
        """Continuation rows merge into the previous case."""
        rows = [
            {
                "entry_number": 1,
                "case_info": "2024-00001 Alpha v. Beta",
                "ruling_text": "The motion is",
            },
            {
                "entry_number": None,
                "case_info": "",
                "ruling_text": "GRANTED with conditions.",
            },
        ]
        rulings = _join_page_rows(rows)
        assert len(rulings) == 1
        assert "The motion is\nGRANTED with conditions." == rulings[0].ruling_text

    def test_cross_page_continuation(self) -> None:
        """Case spanning pages: continuation from previous page then new case."""
        rows = [
            # Continuation from previous page (no entry number).
            {
                "entry_number": None,
                "case_info": "",
                "ruling_text": "...remaining text from previous case.",
            },
            # New case starts.
            {
                "entry_number": 3,
                "case_info": "2024-00999999 Thompson v. City",
                "ruling_text": "OVERRULED.",
            },
        ]
        rulings = _join_page_rows(rows)
        assert len(rulings) == 2
        assert rulings[0].ruling_text == "...remaining text from previous case."
        assert rulings[1].extracted_case_number == "2024-00999999"

    def test_metadata_applied(self) -> None:
        """Metadata (judge_name, department, hearing_date) is applied to all rulings."""
        rows = [
            {"entry_number": 1, "case_info": "2024-00001 Alpha v. Beta", "ruling_text": "GRANTED."},
        ]
        metadata = {"judge_name": "Test Judge", "department": "C25", "hearing_date": "2026-03-01"}
        rulings = _join_page_rows(rows, metadata=metadata)
        assert rulings[0].extracted_judge_name == "Test Judge"
        assert rulings[0].department == "C25"
        assert rulings[0].hearing_date == "2026-03-01"

    def test_empty_rows_returns_empty(self) -> None:
        """Empty row list returns empty ruling list."""
        assert _join_page_rows([]) == []

    def test_case_info_merging_for_continuation(self) -> None:
        """Continuation case_info is appended to previous case."""
        rows = [
            {"entry_number": 1, "case_info": "2024-00001 Alpha v. Beta", "ruling_text": "Part 1"},
            {"entry_number": None, "case_info": "Additional info", "ruling_text": "Part 2"},
        ]
        rulings = _join_page_rows(rows)
        assert len(rulings) == 1
        assert "Additional info" in rulings[0].extracted_case_title

    def test_header_rows_skipped(self) -> None:
        """Header rows (null entry_number, empty ruling_text) are filtered out."""
        rows = [
            # Header row — should be skipped.
            {
                "entry_number": None,
                "case_info": "Department C25 - Hon. Jane Doe",
                "ruling_text": "",
            },
            # Real case.
            {
                "entry_number": 1,
                "case_info": "2024-00001 Alpha v. Beta",
                "ruling_text": "GRANTED.",
            },
        ]
        rulings = _join_page_rows(rows)
        assert len(rulings) == 1
        assert rulings[0].extracted_case_number == "2024-00001"
        # Header text should NOT appear in the case title.
        assert "Department C25" not in (rulings[0].extracted_case_title or "")

    def test_header_between_cases_skipped(self) -> None:
        """Header row between real cases does not corrupt either case."""
        rows = [
            {
                "entry_number": 1,
                "case_info": "2024-00001 Alpha v. Beta",
                "ruling_text": "GRANTED.",
            },
            # Mid-page header.
            {
                "entry_number": None,
                "case_info": "Department C25 - Hon. Jane Doe",
                "ruling_text": "",
            },
            {
                "entry_number": 2,
                "case_info": "2024-00002 Gamma v. Delta",
                "ruling_text": "DENIED.",
            },
        ]
        rulings = _join_page_rows(rows)
        assert len(rulings) == 2
        assert "Department C25" not in (rulings[0].extracted_case_title or "")
        assert rulings[1].extracted_case_number == "2024-00002"


# ---------------------------------------------------------------------------
# Calendar header detection tests (#2096)
# ---------------------------------------------------------------------------


class TestIsCalendarHeader:
    """Tests for _is_calendar_header function (#2096)."""

    def test_oc_department_header(self) -> None:
        """Standard OC calendar header with department info."""
        text = (
            "Superior Court of the State of California County of Orange "
            "TENTATIVE RULINGS DEPARTMENT C27 Judge Thomas S. McConville "
            "February 23, 2026 at 2:00 p.m."
        )
        assert _is_calendar_header(text) is True

    def test_dept_abbreviation(self) -> None:
        """Header using DEPT. abbreviation."""
        text = "TENTATIVE RULINGS DEPT. C28 Judge Jane Smith March 1, 2026"
        assert _is_calendar_header(text) is True

    def test_dept_no_period(self) -> None:
        """Header using DEPT without period."""
        text = "TENTATIVE RULINGS DEPT C25 Hearing Date: March 1, 2026"
        assert _is_calendar_header(text) is True

    def test_ruling_singular(self) -> None:
        """Header with singular RULING."""
        text = "TENTATIVE RULING DEPARTMENT C10 Judge Bob Johnson"
        assert _is_calendar_header(text) is True

    def test_normal_ruling_text(self) -> None:
        """Normal ruling text is not a calendar header."""
        text = (
            "The motion for summary adjudication is DENIED. "
            "Defendant has not met their burden of proof."
        )
        assert _is_calendar_header(text) is False

    def test_empty_text(self) -> None:
        """Empty text is not a calendar header."""
        assert _is_calendar_header("") is False
        assert _is_calendar_header(None) is False

    def test_text_mentioning_department_in_ruling(self) -> None:
        """Ruling text that mentions a department in context is not a header."""
        text = "The Court finds that Defendant's motion to transfer to Department C25 is DENIED."
        assert _is_calendar_header(text) is False

    def test_numeric_department(self) -> None:
        """Header with purely numeric department (e.g., DEPARTMENT 12)."""
        text = "TENTATIVE RULINGS DEPARTMENT 12 Judge Smith"
        assert _is_calendar_header(text) is True


class TestDeduplicateRulingTexts:
    """Tests for _deduplicate_ruling_texts function (#2096)."""

    def test_no_duplicates(self) -> None:
        """Unique ruling texts are not changed."""
        from framework.llm_schema import ExtractedRuling

        rulings = [
            ExtractedRuling(
                extracted_case_number="2024-00001",
                ruling_text="A" * 300,
            ),
            ExtractedRuling(
                extracted_case_number="2024-00002",
                ruling_text="B" * 300,
            ),
        ]
        result = _deduplicate_ruling_texts(rulings)
        assert result[0].ruling_text is not None
        assert result[1].ruling_text is not None

    def test_duplicate_texts_nulled(self) -> None:
        """Duplicate ruling texts are nulled on subsequent occurrences."""
        from framework.llm_schema import ExtractedRuling

        same_text = "X" * 300
        rulings = [
            ExtractedRuling(
                extracted_case_number="2024-00001",
                ruling_text=same_text,
            ),
            ExtractedRuling(
                extracted_case_number="2024-00002",
                ruling_text=same_text,
            ),
            ExtractedRuling(
                extracted_case_number="2024-00003",
                ruling_text=same_text,
            ),
        ]
        result = _deduplicate_ruling_texts(rulings)
        assert result[0].ruling_text == same_text
        assert result[1].ruling_text is None
        assert result[2].ruling_text is None

    def test_short_duplicates_exempt(self) -> None:
        """Short duplicate texts (< 200 chars) are not deduplicated."""
        from framework.llm_schema import ExtractedRuling

        short_text = "GRANTED."
        rulings = [
            ExtractedRuling(
                extracted_case_number="2024-00001",
                ruling_text=short_text,
            ),
            ExtractedRuling(
                extracted_case_number="2024-00002",
                ruling_text=short_text,
            ),
        ]
        result = _deduplicate_ruling_texts(rulings)
        assert result[0].ruling_text == short_text
        assert result[1].ruling_text == short_text

    def test_none_texts_ignored(self) -> None:
        """Rulings with None text are not considered for deduplication."""
        from framework.llm_schema import ExtractedRuling

        rulings = [
            ExtractedRuling(
                extracted_case_number="2024-00001",
                ruling_text=None,
            ),
            ExtractedRuling(
                extracted_case_number="2024-00002",
                ruling_text="A" * 300,
            ),
        ]
        result = _deduplicate_ruling_texts(rulings)
        assert result[0].ruling_text is None
        assert result[1].ruling_text is not None

    def test_preserves_all_fields(self) -> None:
        """Deduplication preserves ALL non-ruling_text fields including parties, outcome, etc."""
        from framework.llm_schema import (
            ExtractedParty,
            ExtractedRuling,
            ExtractionOutcome,
            FieldConfidence,
        )

        same_text = "Y" * 300
        parties = [ExtractedParty(name="Smith", role="plaintiff")]
        confidence = FieldConfidence(case_number="high", ruling_text="high")
        rulings = [
            ExtractedRuling(
                extracted_case_number="2024-00001",
                extracted_case_title="Alpha v. Beta",
                extracted_judge_name="Judge Smith",
                department="C25",
                hearing_date="2026-03-01",
                ruling_text=same_text,
                extracted_parties=parties,
                motion_type="msj",
                outcome=ExtractionOutcome.GRANTED,
                case_type="civil",
                confidence=confidence,
            ),
            ExtractedRuling(
                extracted_case_number="2024-00002",
                extracted_case_title="Gamma v. Delta",
                extracted_judge_name="Judge Smith",
                department="C25",
                hearing_date="2026-03-01",
                ruling_text=same_text,
                extracted_parties=parties,
                motion_type="demurrer",
                outcome=ExtractionOutcome.DENIED,
                case_type="civil",
                confidence=confidence,
            ),
        ]
        result = _deduplicate_ruling_texts(rulings)
        # First keeps text.
        assert result[0].ruling_text == same_text
        assert result[0].extracted_case_number == "2024-00001"
        assert result[0].extracted_case_title == "Alpha v. Beta"
        # Second is nulled but keeps ALL other fields.
        assert result[1].ruling_text is None
        assert result[1].extracted_case_number == "2024-00002"
        assert result[1].extracted_case_title == "Gamma v. Delta"
        assert result[1].extracted_judge_name == "Judge Smith"
        assert result[1].department == "C25"
        assert result[1].extracted_parties == parties
        assert result[1].motion_type == "demurrer"
        assert result[1].outcome == ExtractionOutcome.DENIED
        assert result[1].case_type == "civil"
        assert result[1].confidence == confidence


# ---------------------------------------------------------------------------
# Calendar-listing-only detection (#2446)
# ---------------------------------------------------------------------------


class TestIsCalendarListingOnly:
    """Tests for _is_calendar_listing_only function (#2446)."""

    # --- Positive cases: these should be classified as calendar listings ---

    def test_off_calendar_marker(self) -> None:
        """Bare OFF-CALENDAR marker is a calendar listing."""
        assert _is_calendar_listing_only("OFF-CALENDAR") is True

    def test_off_calendar_no_hyphen(self) -> None:
        """OFF CALENDAR without hyphen still matches."""
        assert _is_calendar_listing_only("OFF CALENDAR") is True

    def test_off_calendar_mixed_case(self) -> None:
        """Off-Calendar in mixed case still matches."""
        assert _is_calendar_listing_only("Off-Calendar") is True

    def test_off_calendar_trailing_period(self) -> None:
        """OFF-CALENDAR. with trailing period still matches."""
        assert _is_calendar_listing_only("OFF-CALENDAR.") is True

    def test_off_calendar_em_dash(self) -> None:
        """OFF\u2014CALENDAR (em dash) still matches."""
        assert _is_calendar_listing_only("OFF\u2014CALENDAR") is True

    def test_no_tentative_posted_marker(self) -> None:
        """'NO TENTATIVE POSTED' is a calendar-listing marker (#2446)."""
        assert _is_calendar_listing_only("NO TENTATIVE POSTED") is True

    def test_no_tentative_alone(self) -> None:
        """Bare 'NO TENTATIVE' is a calendar-listing marker."""
        assert _is_calendar_listing_only("NO TENTATIVE") is True

    def test_no_tentative_mixed_case(self) -> None:
        """'No Tentative Posted' in mixed case still matches."""
        assert _is_calendar_listing_only("No Tentative Posted") is True

    def test_no_tentative_trailing_period(self) -> None:
        """'No tentative posted.' with trailing period still matches."""
        assert _is_calendar_listing_only("No tentative posted.") is True

    def test_motion_to_strike_only(self) -> None:
        """Just a motion-type label is a calendar listing."""
        assert _is_calendar_listing_only("Motion to Strike") is True

    def test_demurrer_only(self) -> None:
        """Just 'Demurrer' is a calendar listing."""
        assert _is_calendar_listing_only("Demurrer") is True

    def test_demurrer_to_fac(self) -> None:
        """'Demurrer to First Amended Complaint' is a calendar listing."""
        assert _is_calendar_listing_only("Demurrer to First Amended Complaint") is True

    def test_demurrers_plural(self) -> None:
        """Plural 'Demurrers' is still a calendar listing (#2446)."""
        assert _is_calendar_listing_only("Demurrers") is True

    def test_hearings_plural(self) -> None:
        """Plural 'Hearings on Motion' is a calendar listing (#2446)."""
        assert _is_calendar_listing_only("Hearings on Motions") is True

    def test_applications_plural(self) -> None:
        """Plural 'Applications for Fees' is a calendar listing (#2446)."""
        assert _is_calendar_listing_only("Applications for Attorney Fees") is True

    def test_motion_type_with_trailing_period(self) -> None:
        """A motion-type label with a trailing period is a calendar listing (#2446)."""
        assert _is_calendar_listing_only("Motion to Strike.") is True

    def test_motion_type_with_internal_periods(self) -> None:
        """Motion labels with internal periods (e.g. N.O.V.) are calendar listings."""
        assert _is_calendar_listing_only("Motion for Judgment N.O.V.") is True

    def test_demurrer_with_trailing_period(self) -> None:
        """Demurrer with trailing period is a calendar listing (#2446)."""
        assert _is_calendar_listing_only("Demurrer to Complaint.") is True

    def test_multiline_motions(self) -> None:
        """Multiple motion labels on separate lines are a calendar listing."""
        assert _is_calendar_listing_only("Demurrer\nMotion to Strike") is True

    def test_motion_for_attorneys_fees(self) -> None:
        """'Motion for Attorneys' Fees' is a calendar listing."""
        assert _is_calendar_listing_only("Motion for Attorneys' Fees") is True

    def test_motion_for_summary_judgment(self) -> None:
        """'Motion for Summary Judgment' alone (no disposition) is a listing."""
        assert _is_calendar_listing_only("Motion for Summary Judgment") is True

    def test_case_management_conference(self) -> None:
        """Case Management Conference alone is a calendar listing."""
        assert _is_calendar_listing_only("Case Management Conference") is True

    def test_ex_parte_application(self) -> None:
        """Ex Parte Application to ... alone is a calendar listing."""
        assert _is_calendar_listing_only("Ex Parte Application to Continue") is True

    def test_osc(self) -> None:
        """OSC re contempt alone is a calendar listing."""
        assert _is_calendar_listing_only("OSC re Sanctions") is True

    # --- Negative cases: these are real rulings, must NOT be dropped ---

    def test_real_short_grant(self) -> None:
        """Motion to strike is GRANTED — real ruling with disposition."""
        assert _is_calendar_listing_only("The motion is GRANTED.") is False

    def test_real_short_denied(self) -> None:
        """'Denied.' alone counts as a ruling (has disposition verb)."""
        assert _is_calendar_listing_only("Denied.") is False

    def test_real_short_sustained(self) -> None:
        """Demurrer SUSTAINED — real ruling."""
        assert _is_calendar_listing_only("Demurrer is SUSTAINED without leave.") is False

    def test_real_short_overruled(self) -> None:
        """Demurrer OVERRULED — real ruling."""
        assert _is_calendar_listing_only("Demurrer OVERRULED.") is False

    def test_real_continued(self) -> None:
        """Motion CONTINUED to a later date — real ruling."""
        assert _is_calendar_listing_only("Motion CONTINUED to April 1, 2026.") is False

    def test_none_text(self) -> None:
        """None text is not a calendar listing (preserve for other filters)."""
        assert _is_calendar_listing_only(None) is False

    def test_empty_text(self) -> None:
        """Empty string is not a calendar listing."""
        assert _is_calendar_listing_only("") is False

    def test_whitespace_only(self) -> None:
        """Whitespace-only text is not a calendar listing."""
        assert _is_calendar_listing_only("   \n\t  ") is False

    def test_long_substantive_text(self) -> None:
        """Text longer than the calendar-listing threshold is never dropped."""
        # 200+ chars of real ruling reasoning, no short-listing match.
        text = (
            "The court has reviewed the moving and opposition papers and finds "
            "that Defendant has met the initial burden on summary judgment. "
            "Plaintiff has failed to produce admissible evidence to raise a "
            "triable issue of material fact. The motion is GRANTED."
        )
        assert _is_calendar_listing_only(text) is False

    def test_long_text_no_disposition(self) -> None:
        """Long text without a disposition verb is still not a listing if > threshold.

        The threshold check prevents us from dropping a substantive ruling
        that happens to lack an explicit disposition keyword.  This is
        critical for correctness — better to keep ambiguous rows than drop
        them.
        """
        # 150 chars — over the 100-char threshold.
        text = (
            "The court has reviewed all papers submitted by the parties "
            "including the recent supplemental brief filed after oral "
            "argument on the matter presented."
        )
        assert _is_calendar_listing_only(text) is False

    def test_mixed_valid_and_motion_line_with_disposition(self) -> None:
        """If any disposition verb is present, text is a real ruling."""
        text = "Motion to Strike is DENIED for the reasons stated."
        assert _is_calendar_listing_only(text) is False

    def test_ruling_text_mentions_motion_but_has_no_disposition(self) -> None:
        """Pure motion label with no disposition IS a listing (short)."""
        assert _is_calendar_listing_only("Motion to Compel Further") is True

    def test_motion_label_with_appointment(self) -> None:
        """Petition to Approve is a calendar listing."""
        assert _is_calendar_listing_only("Petition to Approve Compromise") is True

    def test_motion_granted_word_embedded_in_name(self) -> None:
        """
        A party name or word like 'Grantor' does not count as a disposition
        because the regex matches whole words only.  Here the text has the
        literal substring "grant" inside "Grantham" but no full-word match.
        """
        # Pure short motion label with party-name-like text — still a listing.
        text = "Motion to Strike Grantham's Answer"
        assert _is_calendar_listing_only(text) is True


class TestIsCalendarListingOnlyWidened2489:
    """Additional widened-filter cases for #2489.

    Covers:
    - ``O/C`` abbreviation for Off Calendar.
    - Bare parenthetical disposition markers (``(Moot)``, ``(Continued)`` …).
    - Bare continuance-only lines (``Cont. To 4/20``, ``CONTINUED TO 10/6/26``).
    - Placeholder text (``Tentative pending.``, ``ADR Review Hearing``).
    - Party-prefixed motion-type labels (``Plaintiff's Motion for …``).
    - Demurrer with parenthetical scope.
    """

    # --- O/C abbreviation ---

    def test_oc_abbreviation(self) -> None:
        """``O/C`` is an OC shorthand for Off Calendar (#2489)."""
        assert _is_calendar_listing_only("O/C") is True

    def test_oc_abbreviation_lower(self) -> None:
        """``o/c`` lowercase still matches (#2489)."""
        assert _is_calendar_listing_only("o/c") is True

    def test_oc_abbreviation_spaced(self) -> None:
        """``O / C`` with spaces around the slash still matches (#2489)."""
        assert _is_calendar_listing_only("O / C") is True

    def test_oc_abbreviation_trailing_period(self) -> None:
        """``O/C.`` with trailing period still matches (#2489)."""
        assert _is_calendar_listing_only("O/C.") is True

    def test_oc_abbreviation_inside_longer_text_not_matched_as_oc(self) -> None:
        """``GROUP O/C PROCEEDING`` should NOT be treated as O/C alone.

        The abbreviation only counts as a calendar-listing marker when it is
        the entire stripped text (or the dominant content), not when it
        appears embedded in a longer phrase.  This test documents the
        boundary: a longer, non-listing phrase must not be dropped just
        because it contains "o/c" as a substring.
        """
        # >100 chars so length cap would reject anyway; this is a belt-and-
        # suspenders check that embedded O/C does not bypass the length cap.
        text = (
            "GROUP O/C PROCEEDING has been scheduled for hearing on the "
            "merits — counsel shall appear remotely and be prepared to "
            "argue per the court's calendar."
        )
        assert _is_calendar_listing_only(text) is False

    # --- Bare parenthetical disposition ---

    def test_bare_parenthetical_moot(self) -> None:
        """``(Moot)`` alone is a calendar listing (#2489)."""
        assert _is_calendar_listing_only("(Moot)") is True

    def test_bare_parenthetical_continued(self) -> None:
        """``(Continued)`` alone is a calendar listing (#2489)."""
        assert _is_calendar_listing_only("(Continued)") is True

    def test_bare_parenthetical_withdrawn(self) -> None:
        """``(Withdrawn)`` alone is a calendar listing (#2489)."""
        assert _is_calendar_listing_only("(Withdrawn)") is True

    def test_bare_parenthetical_vacated(self) -> None:
        """``(Vacated)`` alone is a calendar listing (#2489)."""
        assert _is_calendar_listing_only("(Vacated)") is True

    def test_bare_parenthetical_off_calendar(self) -> None:
        """``(Off Calendar)`` alone is a calendar listing (#2489)."""
        assert _is_calendar_listing_only("(Off Calendar)") is True

    def test_bare_parenthetical_mixed_case(self) -> None:
        """``(moot)`` lowercase still matches (#2489)."""
        assert _is_calendar_listing_only("(moot)") is True

    def test_bare_parenthetical_trailing_period(self) -> None:
        """``(Continued).`` with trailing period still matches (#2489)."""
        assert _is_calendar_listing_only("(Continued).") is True

    def test_parenthetical_disposition_with_surrounding_ruling_not_dropped(
        self,
    ) -> None:
        """Parenthetical inside a real ruling must not drop the ruling."""
        text = "The court GRANTED the motion (Continued to 4/20 for argument) and ordered briefing."
        assert _is_calendar_listing_only(text) is False

    # --- Bare continuance-only lines ---

    def test_bare_continuance_cont_to_short_date(self) -> None:
        """``Cont. To 4/20`` is a bare continuance listing (#2489)."""
        assert _is_calendar_listing_only("Cont. To 4/20") is True

    def test_bare_continuance_cont_to_full_date(self) -> None:
        """``Cont. to 4/20/2026`` is a bare continuance listing (#2489)."""
        assert _is_calendar_listing_only("Cont. to 4/20/2026") is True

    def test_bare_continuance_continued_to_upper(self) -> None:
        """``CONTINUED TO 10/6/26`` is a bare continuance listing (#2489).

        Note: ``CONTINUED`` is a disposition verb that normally keeps the
        text classified as a real ruling, but a bare-line-only continuance
        with no other content should still be dropped as a listing.
        """
        assert _is_calendar_listing_only("CONTINUED TO 10/6/26") is True

    def test_bare_continuance_month_name_date(self) -> None:
        """``Continued to April 20, 2026`` (bare) is a listing (#2489)."""
        assert _is_calendar_listing_only("Continued to April 20, 2026") is True

    def test_bare_continuance_with_at_time(self) -> None:
        """``Cont. to 4/20 at 9:00 a.m.`` is a bare continuance listing."""
        assert _is_calendar_listing_only("Cont. to 4/20 at 9:00 a.m.") is True

    def test_continued_ruling_not_dropped(self) -> None:
        """A real ruling that uses ``CONTINUED`` as a verb is preserved."""
        text = "Motion CONTINUED to April 1, 2026 for further briefing."
        # Over the bare-continuance line pattern (extra trailing content),
        # so the disposition-verb branch preserves it.
        assert _is_calendar_listing_only(text) is False

    # --- Placeholder text ---

    def test_tentative_pending(self) -> None:
        """``Tentative pending.`` is a placeholder (#2489)."""
        assert _is_calendar_listing_only("Tentative pending.") is True

    def test_tentative_pending_no_period(self) -> None:
        """``Tentative pending`` without a period still matches (#2489)."""
        assert _is_calendar_listing_only("Tentative pending") is True

    def test_tentative_pending_mixed_case(self) -> None:
        """``TENTATIVE PENDING`` uppercase still matches (#2489)."""
        assert _is_calendar_listing_only("TENTATIVE PENDING") is True

    def test_adr_review_hearing(self) -> None:
        """``ADR Review Hearing`` is a placeholder listing (#2489)."""
        assert _is_calendar_listing_only("ADR Review Hearing") is True

    def test_adr_review(self) -> None:
        """Bare ``ADR Review`` is a placeholder listing (#2489)."""
        assert _is_calendar_listing_only("ADR Review") is True

    # --- Party-prefixed motion-type labels ---

    def test_plaintiff_motion_for_approval(self) -> None:
        """``Plaintiff's Motion for Approval`` is a listing (#2489)."""
        assert _is_calendar_listing_only("Plaintiff's Motion for Approval") is True

    def test_defendant_motion_for_bifurcation(self) -> None:
        """``Defendant's Motion for Bifurcation`` is a listing (#2489)."""
        assert _is_calendar_listing_only("Defendant's Motion for Bifurcation") is True

    def test_plaintiffs_plural_motion(self) -> None:
        """``Plaintiffs' Motion for Sanctions`` (plural) is a listing (#2489)."""
        assert _is_calendar_listing_only("Plaintiffs' Motion for Sanctions") is True

    def test_cross_complainant_motion(self) -> None:
        """``Cross-Complainant's Motion to Compel`` is a listing (#2489)."""
        assert _is_calendar_listing_only("Cross-Complainant's Motion to Compel") is True

    def test_cross_defendant_motion(self) -> None:
        """``Cross-Defendant's Motion for Summary Judgment`` is a listing."""
        assert _is_calendar_listing_only("Cross-Defendant's Motion for Summary Judgment") is True

    def test_petitioner_motion(self) -> None:
        """``Petitioner's Motion to Amend`` is a listing (#2489)."""
        assert _is_calendar_listing_only("Petitioner's Motion to Amend") is True

    def test_respondent_motion(self) -> None:
        """``Respondent's Motion to Quash`` is a listing (#2489)."""
        assert _is_calendar_listing_only("Respondent's Motion to Quash") is True

    def test_party_prefix_with_curly_apostrophe(self) -> None:
        """Curly apostrophe (\u2019) variant of party prefix matches (#2489)."""
        # U+2019 RIGHT SINGLE QUOTATION MARK — common in LLM output.
        text = "Defendant\u2019s Motion to Dismiss"
        assert _is_calendar_listing_only(text) is True

    def test_party_prefix_demurrer(self) -> None:
        """``Plaintiff's Demurrer to Answer`` is a listing (#2489)."""
        assert _is_calendar_listing_only("Plaintiff's Demurrer to Answer") is True

    def test_party_prefixed_real_ruling_not_dropped(self) -> None:
        """``Plaintiff's Motion is GRANTED`` is a real ruling (disposition)."""
        text = "Plaintiff's Motion for Summary Judgment is GRANTED."
        assert _is_calendar_listing_only(text) is False

    # --- Demurrer with parenthetical scope ---

    def test_demurrer_with_parenthetical_scope(self) -> None:
        """``Demurrer (re First Amended Complaint)`` is a listing (#2489)."""
        assert _is_calendar_listing_only("Demurrer (re First Amended Complaint)") is True

    def test_demurrer_with_parenthetical_scope_spaced(self) -> None:
        """``Demurrer ( re First Amended Complaint)`` (space after paren) matches.

        The regex originally used a misplaced word-boundary ``\\b`` between
        the alternation ``(?:to|\\()`` and the trailing ``.*``, which
        incorrectly required a word-boundary transition between ``(`` and
        the next character.  That failed on formatting variants with a
        space after the opening parenthesis.  Adversarial Gemini reviewer
        flagged this; this test pins the fix.
        """
        assert _is_calendar_listing_only("Demurrer ( re First Amended Complaint)") is True


class TestDropCalendarListingRulings:
    """Tests for _drop_calendar_listing_rulings function (#2446)."""

    def test_drops_off_calendar_entry(self) -> None:
        """OFF-CALENDAR rows are filtered out."""
        rulings = [
            ExtractedRuling(
                extracted_case_number="30-2024-00001",
                extracted_case_title="Anderson vs. Schreiffer",
                ruling_text="OFF-CALENDAR",
            ),
            ExtractedRuling(
                extracted_case_number="30-2024-00002",
                extracted_case_title="Smith vs. Jones",
                ruling_text="Demurrer is SUSTAINED. Leave to amend granted.",
            ),
        ]
        result = _drop_calendar_listing_rulings(rulings)
        assert len(result) == 1
        assert result[0].extracted_case_number == "30-2024-00002"

    def test_drops_no_tentative_posted_entry(self) -> None:
        """'NO TENTATIVE POSTED' rows are filtered out (#2446)."""
        rulings = [
            ExtractedRuling(
                extracted_case_number="30-2024-00001",
                extracted_case_title="Acme v. Widgets",
                ruling_text="NO TENTATIVE POSTED",
            ),
            ExtractedRuling(
                extracted_case_number="30-2024-00002",
                extracted_case_title="Smith vs. Jones",
                ruling_text="Demurrer is SUSTAINED. Leave to amend granted.",
            ),
        ]
        result = _drop_calendar_listing_rulings(rulings)
        assert len(result) == 1
        assert result[0].extracted_case_number == "30-2024-00002"

    def test_drops_motion_type_only_entry(self) -> None:
        """Rows whose ruling_text is only a motion label are filtered out."""
        rulings = [
            ExtractedRuling(
                extracted_case_number="30-2024-00001",
                ruling_text="Motion for Attorneys' Fees",
            ),
            ExtractedRuling(
                extracted_case_number="30-2024-00002",
                ruling_text="Motion is GRANTED on the merits. " * 5,
            ),
        ]
        result = _drop_calendar_listing_rulings(rulings)
        assert len(result) == 1
        assert result[0].extracted_case_number == "30-2024-00002"

    def test_preserves_cross_reference_entries(self) -> None:
        """Entries with cross_reference_source set are exempt from the filter."""
        rulings = [
            ExtractedRuling(
                extracted_case_number="30-2024-00001",
                ruling_text="Demurrer",  # would normally be classified as listing
                cross_reference_source=2,
            ),
        ]
        result = _drop_calendar_listing_rulings(rulings)
        assert len(result) == 1
        assert result[0].extracted_case_number == "30-2024-00001"

    def test_preserves_none_text_entries(self) -> None:
        """Entries whose ruling_text is None are preserved (handled elsewhere)."""
        rulings = [
            ExtractedRuling(
                extracted_case_number="30-2024-00001",
                extracted_case_title="Alpha v. Beta",
                ruling_text=None,
            ),
        ]
        result = _drop_calendar_listing_rulings(rulings)
        assert len(result) == 1
        assert result[0].extracted_case_number == "30-2024-00001"

    def test_preserves_empty_text_entries(self) -> None:
        """Entries whose ruling_text is empty string are preserved."""
        rulings = [
            ExtractedRuling(
                extracted_case_number="30-2024-00001",
                ruling_text="",
            ),
        ]
        result = _drop_calendar_listing_rulings(rulings)
        assert len(result) == 1

    def test_preserves_real_rulings(self) -> None:
        """Substantive rulings with dispositions are never dropped."""
        rulings = [
            ExtractedRuling(
                extracted_case_number="30-2024-00001",
                ruling_text="Demurrer is SUSTAINED without leave to amend.",
            ),
            ExtractedRuling(
                extracted_case_number="30-2024-00002",
                ruling_text="The motion for summary judgment is DENIED.",
            ),
        ]
        result = _drop_calendar_listing_rulings(rulings)
        assert len(result) == 2

    def test_empty_list(self) -> None:
        """Empty input returns empty output."""
        assert _drop_calendar_listing_rulings([]) == []

    def test_all_calendar_listings_dropped(self) -> None:
        """All-calendar-listing input returns empty list."""
        rulings = [
            ExtractedRuling(
                extracted_case_number="30-2024-00001",
                ruling_text="OFF-CALENDAR",
            ),
            ExtractedRuling(
                extracted_case_number="30-2024-00002",
                ruling_text="Demurrer\nMotion to Strike",
            ),
            ExtractedRuling(
                extracted_case_number="30-2024-00003",
                ruling_text="Motion to Compel Further Discovery",
            ),
        ]
        result = _drop_calendar_listing_rulings(rulings)
        assert result == []

    def test_drops_widened_patterns_integration(self) -> None:
        """All widened #2489 patterns are dropped end-to-end.

        One ruling per new pattern:
        - ``O/C`` abbreviation
        - Bare parenthetical ``(Moot)``
        - Bare continuance ``CONTINUED TO 10/6/26``
        - Placeholder ``Tentative pending.``
        - Party-prefixed ``Plaintiff's Motion for Approval``
        - Demurrer with parenthetical ``Demurrer (re First Amended Complaint)``

        Plus one real ruling that must be preserved.
        """
        rulings = [
            ExtractedRuling(
                extracted_case_number="30-2024-00001",
                ruling_text="O/C",
            ),
            ExtractedRuling(
                extracted_case_number="30-2024-00002",
                ruling_text="(Moot)",
            ),
            ExtractedRuling(
                extracted_case_number="30-2024-00003",
                ruling_text="CONTINUED TO 10/6/26",
            ),
            ExtractedRuling(
                extracted_case_number="30-2024-00004",
                ruling_text="Tentative pending.",
            ),
            ExtractedRuling(
                extracted_case_number="30-2024-00005",
                ruling_text="Plaintiff's Motion for Approval",
            ),
            ExtractedRuling(
                extracted_case_number="30-2024-00006",
                ruling_text="Demurrer (re First Amended Complaint)",
            ),
            ExtractedRuling(
                extracted_case_number="30-2024-00007",
                ruling_text="The motion for summary judgment is GRANTED.",
            ),
        ]
        result = _drop_calendar_listing_rulings(rulings)
        assert len(result) == 1
        assert result[0].extracted_case_number == "30-2024-00007"


class TestJoinPageRowsCalendarListing:
    """Integration tests: _join_page_rows drops calendar listings (#2446)."""

    def test_w8_calendar_pdf_produces_zero_rulings(self) -> None:
        """A W8-style calendar with only motion-type cells produces 0 rulings.

        Simulates the Dept W8 March 13, 2026 calendar where every row is a
        case+motion listing with no actual ruling body.  The expected
        behavior is that _join_page_rows returns an empty list.
        """
        rows = [
            {
                "entry_number": 1,
                "case_info": "30-2024-00001 Anderson vs. P.K. Schreiffer LLP",
                "ruling_text": "Motion for Attorneys' Fees",
            },
            {
                "entry_number": 2,
                "case_info": "30-2024-00002 Ko vs. Bank of America Corporation",
                "ruling_text": "Demurrer\nMotion to Strike",
            },
            {
                "entry_number": 3,
                "case_info": "30-2024-00003 Smith vs. Jones",
                "ruling_text": "Motion to Compel Further Discovery",
            },
            {
                "entry_number": 4,
                "case_info": "30-2024-00004 Alpha vs. Beta",
                "ruling_text": "OFF-CALENDAR",
            },
        ]
        rulings = _join_page_rows(rows)
        assert rulings == []

    def test_mixed_calendar_and_real_rulings(self) -> None:
        """A mixed page: real rulings kept, calendar listings dropped."""
        rows = [
            {
                "entry_number": 1,
                "case_info": "30-2024-00001 Alpha vs. Beta",
                "ruling_text": "Motion for Attorneys' Fees",  # listing
            },
            {
                "entry_number": 2,
                "case_info": "30-2024-00002 Gamma vs. Delta",
                "ruling_text": "The motion to strike is GRANTED in part.",
            },
            {
                "entry_number": 3,
                "case_info": "30-2024-00003 Epsilon vs. Zeta",
                "ruling_text": "OFF-CALENDAR",  # listing
            },
            {
                "entry_number": 4,
                "case_info": "30-2024-00004 Eta vs. Theta",
                "ruling_text": "Demurrer is OVERRULED as to the first cause.",
            },
        ]
        rulings = _join_page_rows(rows)
        assert len(rulings) == 2
        # Note: the extractor strips the OC "30-" prefix from case numbers.
        assert rulings[0].extracted_case_number == "2024-00002"
        assert rulings[0].ruling_text is not None
        assert "GRANTED" in rulings[0].ruling_text
        assert rulings[1].extracted_case_number == "2024-00004"
        assert rulings[1].ruling_text is not None
        assert "OVERRULED" in rulings[1].ruling_text

    def test_palacios_style_row_dropped(self) -> None:
        """Evidence row 1 (Anderson 26-char listing) is dropped."""
        rows = [
            {
                "entry_number": 1,
                "case_info": "30-2024-00001 Anderson vs. P.K. Schreiffer LLP",
                # 26 chars, motion-type-only — matches the DB evidence.
                "ruling_text": "Motion for Attorneys' Fees",
            },
        ]
        rulings = _join_page_rows(rows)
        assert rulings == []

    def test_ko_bank_of_america_row_dropped(self) -> None:
        """Evidence row 2 (Ko vs. Bank of America 25-char listing) is dropped."""
        rows = [
            {
                "entry_number": 1,
                "case_info": "30-2024-00002 Ko vs. Bank of America Corporation",
                "ruling_text": "Demurrer\nMotion to Strike",
            },
        ]
        rulings = _join_page_rows(rows)
        assert rulings == []

    def test_all_real_rulings_preserved(self) -> None:
        """A page of real rulings is unaffected by the filter."""
        rows = [
            {
                "entry_number": 1,
                "case_info": "30-2024-00001 Alpha vs. Beta",
                "ruling_text": (
                    "The motion for summary judgment is GRANTED. "
                    "Defendant has met its initial burden and Plaintiff has "
                    "failed to raise a triable issue of material fact."
                ),
            },
            {
                "entry_number": 2,
                "case_info": "30-2024-00002 Gamma vs. Delta",
                "ruling_text": (
                    "Demurrer is OVERRULED as to the first cause of action "
                    "and SUSTAINED without leave to amend as to the second."
                ),
            },
        ]
        rulings = _join_page_rows(rows)
        assert len(rulings) == 2
        assert rulings[0].ruling_text is not None
        assert "GRANTED" in rulings[0].ruling_text
        assert rulings[1].ruling_text is not None
        assert "OVERRULED" in rulings[1].ruling_text

    def test_widened_listing_patterns_dropped_via_join(self) -> None:
        """All widened #2489 listing patterns are dropped via _join_page_rows."""
        rows = [
            {
                "entry_number": 1,
                "case_info": "30-2024-00001 Alpha vs. Beta",
                "ruling_text": "O/C",
            },
            {
                "entry_number": 2,
                "case_info": "30-2024-00002 Gamma vs. Delta",
                "ruling_text": "(Withdrawn)",
            },
            {
                "entry_number": 3,
                "case_info": "30-2024-00003 Epsilon vs. Zeta",
                "ruling_text": "Cont. to 4/20",
            },
            {
                "entry_number": 4,
                "case_info": "30-2024-00004 Eta vs. Theta",
                "ruling_text": "Tentative pending.",
            },
            {
                "entry_number": 5,
                "case_info": "30-2024-00005 Iota vs. Kappa",
                "ruling_text": "Defendant's Motion for Bifurcation",
            },
            {
                "entry_number": 6,
                "case_info": "30-2024-00006 Lambda vs. Mu",
                "ruling_text": "The motion is DENIED on the merits.",
            },
        ]
        rulings = _join_page_rows(rows)
        assert len(rulings) == 1
        assert rulings[0].extracted_case_number == "2024-00006"
        assert rulings[0].ruling_text is not None
        assert "DENIED" in rulings[0].ruling_text


class TestJoinPageRowsContamination:
    """Tests for _join_page_rows calendar header filtering and dedup (#2096)."""

    def test_calendar_header_ruling_text_filtered(self) -> None:
        """Ruling text that is actually a calendar header is set to None."""
        rows = [
            {
                "entry_number": 1,
                "case_info": "2024-00001 Smith v. Jones",
                "ruling_text": (
                    "Superior Court of the State of California "
                    "County of Orange TENTATIVE RULINGS DEPARTMENT C27 "
                    "Judge Thomas S. McConville February 23, 2026"
                ),
            },
        ]
        rulings = _join_page_rows(rows)
        assert len(rulings) == 1
        assert rulings[0].ruling_text is None

    def test_duplicate_ruling_text_deduplicated(self) -> None:
        """Duplicate ruling texts across cases are nulled after the first."""
        long_text = ("The motion for summary judgment is hereby DENIED. " * 10).strip()
        rows = [
            {
                "entry_number": 1,
                "case_info": "2024-00001 Alpha v. Beta",
                "ruling_text": long_text,
            },
            {
                "entry_number": 2,
                "case_info": "2024-00002 Gamma v. Delta",
                "ruling_text": long_text,
            },
        ]
        rulings = _join_page_rows(rows)
        assert len(rulings) == 2
        assert rulings[0].ruling_text == long_text
        assert rulings[1].ruling_text is None

    def test_non_contaminated_rulings_unchanged(self) -> None:
        """Normal cases with unique, non-header text are not affected."""
        rows = [
            {
                "entry_number": 1,
                "case_info": "2024-00001 Alpha v. Beta",
                "ruling_text": "The motion is GRANTED.",
            },
            {
                "entry_number": 2,
                "case_info": "2024-00002 Gamma v. Delta",
                "ruling_text": "The demurrer is OVERRULED.",
            },
        ]
        rulings = _join_page_rows(rows)
        assert len(rulings) == 2
        assert rulings[0].ruling_text == "The motion is GRANTED."
        assert rulings[1].ruling_text == "The demurrer is OVERRULED."


# ---------------------------------------------------------------------------
# extract_from_pdf tests (per-page extraction)
# ---------------------------------------------------------------------------


class TestExtractFromPdf:
    """Tests for the multimodal extract_from_pdf method (per-page)."""

    def test_empty_bytes_returns_empty(self) -> None:
        """Empty PDF bytes should return empty list."""
        with patch.object(anthropic, "Anthropic"):
            ext = LlmExtractor(api_key="test-key")
        assert ext.extract_from_pdf(b"") == []

    def test_one_call_per_page(self, sample_pdf_bytes: bytes) -> None:
        """extract_from_pdf sends ONE LLM call per page, not all pages at once."""
        with patch.object(anthropic, "Anthropic"):
            ext = LlmExtractor(api_key="test-key")

        fake_images = [
            (b"\x89PNG_page1", "image/png"),
            (b"\x89PNG_page2", "image/png"),
        ]
        mock_response = _make_llm_response(SINGLE_PAGE_ROWS_JSON)
        with (
            patch(
                "framework.llm_extractor._render_pdf_pages",
                return_value=fake_images,
            ) as mock_render,
            patch(
                "ingestion.llm_providers.call_llm_with_images",
                return_value=mock_response,
            ) as mock_call,
        ):
            ext.extract_from_pdf(sample_pdf_bytes)

        mock_render.assert_called_once_with(sample_pdf_bytes, 50)
        # One call per page = 2 calls for 2 pages.
        assert mock_call.call_count == 2
        # Each call should have exactly one image.
        for call in mock_call.call_args_list:
            images_arg = call.kwargs["images"]
            assert len(images_arg) == 1

    def test_single_page_single_ruling(self, sample_pdf_bytes: bytes) -> None:
        """Single page with one case produces one ruling."""
        with patch.object(anthropic, "Anthropic"):
            ext = LlmExtractor(api_key="test-key")

        mock_response = _make_llm_response(SINGLE_PAGE_ROWS_JSON)
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
        assert rulings[0].extracted_case_number == "2024-01393434"
        assert rulings[0].ruling_text == "The motion for summary adjudication is DENIED."

    def test_multi_case_single_page(self, sample_pdf_bytes: bytes) -> None:
        """Single page with multiple cases produces multiple rulings."""
        with patch.object(anthropic, "Anthropic"):
            ext = LlmExtractor(api_key="test-key")

        mock_response = _make_llm_response(MULTI_CASE_PAGE_ROWS_JSON)
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
        assert rulings[0].extracted_case_number == "2024-01393434"
        assert rulings[1].extracted_case_number == "2024-00567890"

    def test_cross_page_continuation(self, sample_pdf_bytes: bytes) -> None:
        """Cases spanning pages are correctly joined."""
        with patch.object(anthropic, "Anthropic"):
            ext = LlmExtractor(api_key="test-key")

        # Page 1: one complete case.
        page1_response = _make_llm_response(SINGLE_PAGE_ROWS_JSON)
        # Page 2: continuation + new case.
        page2_response = _make_llm_response(CONTINUATION_PAGE_ROWS_JSON)

        with (
            patch(
                "framework.llm_extractor._render_pdf_pages",
                return_value=[
                    (b"\x89PNG_page1", "image/png"),
                    (b"\x89PNG_page2", "image/png"),
                ],
            ),
            patch(
                "ingestion.llm_providers.call_llm_with_images",
                side_effect=[page1_response, page2_response],
            ),
        ):
            rulings = ext.extract_from_pdf(sample_pdf_bytes)

        # Page 1 case + continuation merged, plus page 2 new case = 2 rulings.
        assert len(rulings) == 2
        # First ruling should have merged text.
        assert "DENIED" in rulings[0].ruling_text
        assert "lacks merit" in rulings[0].ruling_text
        # Second ruling is the new case from page 2.
        assert rulings[1].extracted_case_number == "2024-00999999"

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
        """If the LLM API call fails on all pages, returns empty list."""
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

        mock_response = _make_llm_response(SINGLE_PAGE_ROWS_JSON)
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

        mock_response = _make_llm_response(SINGLE_PAGE_ROWS_JSON)
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

        mock_response = _make_llm_response(SINGLE_PAGE_ROWS_JSON)
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

    def test_per_page_prompt_used(self, sample_pdf_bytes: bytes) -> None:
        """The per-page prompt (PDF_PER_PAGE_PROMPT) is used, not the full extraction prompt."""
        from framework.llm_extractor import PDF_PER_PAGE_PROMPT

        with patch.object(anthropic, "Anthropic"):
            ext = LlmExtractor(api_key="test-key")

        mock_response = _make_llm_response(SINGLE_PAGE_ROWS_JSON)
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
        assert call_kwargs["system_prompt"] == PDF_PER_PAGE_PROMPT


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
    """Tests for token usage tracking in the per-page multimodal path."""

    def test_token_usage_logged(self, sample_pdf_bytes: bytes) -> None:
        """Token usage is logged after multimodal extraction."""
        with patch.object(anthropic, "Anthropic"):
            ext = LlmExtractor(api_key="test-key")

        mock_response = _make_llm_response(SINGLE_PAGE_ROWS_JSON)
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

    def test_token_usage_aggregated_across_pages(self, sample_pdf_bytes: bytes) -> None:
        """Token usage is accumulated across all page calls."""
        with patch.object(anthropic, "Anthropic"):
            ext = LlmExtractor(api_key="test-key")

        mock_response = _make_llm_response(SINGLE_PAGE_ROWS_JSON)
        with (
            patch(
                "framework.llm_extractor._render_pdf_pages",
                return_value=[
                    (b"\x89PNG_page1", "image/png"),
                    (b"\x89PNG_page2", "image/png"),
                ],
            ),
            patch(
                "ingestion.llm_providers.call_llm_with_images",
                return_value=mock_response,
            ),
            patch("framework.llm_extractor.logger") as mock_logger,
        ):
            ext.extract_from_pdf(sample_pdf_bytes)

        usage_calls = [
            c
            for c in mock_logger.info.call_args_list
            if c.args and c.args[0] == "llm_extractor.token_usage"
        ]
        assert len(usage_calls) == 1
        # 2 pages * 500 input tokens each = 1000 total.
        assert usage_calls[0].kwargs["input_tokens"] == 1000
        assert usage_calls[0].kwargs["output_tokens"] == 400
        assert usage_calls[0].kwargs["api_calls"] == 2


# ---------------------------------------------------------------------------
# Retry and error handling in _extract_single_page
# ---------------------------------------------------------------------------


class TestExtractSinglePageRetry:
    """Tests for retry and error handling in the per-page extraction path."""

    def test_retries_on_none_response(self, sample_pdf_bytes: bytes) -> None:
        """When LLM returns None for a page, retries before giving up."""
        with patch.object(anthropic, "Anthropic"):
            ext = LlmExtractor(api_key="test-key")
        ext._base_delay = 0.01
        ext._max_retries = 3

        mock_response = _make_llm_response(SINGLE_PAGE_ROWS_JSON)
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

        mock_response = _make_llm_response(SINGLE_PAGE_ROWS_JSON)
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

    def test_partial_page_failure(self, sample_pdf_bytes: bytes) -> None:
        """If one page fails, other pages' results are still used."""
        with patch.object(anthropic, "Anthropic"):
            ext = LlmExtractor(api_key="test-key")
        ext._base_delay = 0.01
        ext._max_retries = 1

        page1_response = _make_llm_response(SINGLE_PAGE_ROWS_JSON)
        with (
            patch(
                "framework.llm_extractor._render_pdf_pages",
                return_value=[
                    (b"\x89PNG_page1", "image/png"),
                    (b"\x89PNG_page2", "image/png"),
                ],
            ),
            patch(
                "ingestion.llm_providers.call_llm_with_images",
                side_effect=[page1_response, None],
            ),
        ):
            rulings = ext.extract_from_pdf(sample_pdf_bytes)

        # Page 1 succeeded, page 2 failed — should still get page 1 results.
        assert len(rulings) == 1


# ---------------------------------------------------------------------------
# _build_user_message_for_page
# ---------------------------------------------------------------------------


class TestBuildUserMessageForPage:
    """Tests for the per-page extraction text message builder."""

    def test_no_metadata(self) -> None:
        """Without metadata, produces a generic extraction message."""
        msg = LlmExtractor._build_user_message_for_page(None)
        assert "Extract all tentative rulings" in msg

    def test_with_all_metadata(self) -> None:
        """With all metadata keys, includes them in the message."""
        msg = LlmExtractor._build_user_message_for_page(
            {
                "judge_name": "Test Judge",
                "department": "D99",
                "hearing_date": "2026-03-01",
            }
        )
        assert "Test Judge" in msg
        assert "D99" in msg
        assert "2026-03-01" in msg

    def test_with_hearing_date_only(self) -> None:
        """With only hearing_date, includes it in the message."""
        msg = LlmExtractor._build_user_message_for_page({"hearing_date": "2026-04-15"})
        assert "2026-04-15" in msg


# ---------------------------------------------------------------------------
# _build_user_message_for_images (backward compat)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Per-page prompt content
# ---------------------------------------------------------------------------


class TestPerPagePrompt:
    """Verify the per-page prompt is well-formed."""

    def test_per_page_prompt_exists(self) -> None:
        """PDF_PER_PAGE_PROMPT is a non-empty string."""
        from framework.llm_extractor import PDF_PER_PAGE_PROMPT

        assert isinstance(PDF_PER_PAGE_PROMPT, str)
        assert len(PDF_PER_PAGE_PROMPT) > 100

    def test_per_page_prompt_describes_output_fields(self) -> None:
        """The per-page prompt describes all output fields."""
        from framework.llm_extractor import PDF_PER_PAGE_PROMPT

        assert "entry_number" in PDF_PER_PAGE_PROMPT
        assert "case_number" in PDF_PER_PAGE_PROMPT
        assert "case_title" in PDF_PER_PAGE_PROMPT
        assert "ruling_text" in PDF_PER_PAGE_PROMPT
        assert "page_header" in PDF_PER_PAGE_PROMPT

    def test_per_page_prompt_requests_json_output(self) -> None:
        """The per-page prompt asks for JSON output with rulings key."""
        from framework.llm_extractor import PDF_PER_PAGE_PROMPT

        assert "rulings" in PDF_PER_PAGE_PROMPT

    def test_per_page_prompt_handles_multiple_layouts(self) -> None:
        """The per-page prompt describes handling for various PDF layouts."""
        from framework.llm_extractor import PDF_PER_PAGE_PROMPT

        assert "Tables" in PDF_PER_PAGE_PROMPT
        assert "Bordered boxes" in PDF_PER_PAGE_PROMPT
        assert "Free-form prose" in PDF_PER_PAGE_PROMPT
        assert "Label-value forms" in PDF_PER_PAGE_PROMPT
        assert "Continuation pages" in PDF_PER_PAGE_PROMPT
        assert "Boilerplate-only pages" in PDF_PER_PAGE_PROMPT


# ---------------------------------------------------------------------------
# Cross-reference resolution tests (#2317)
# ---------------------------------------------------------------------------


class TestResolveCrossReferences:
    """Tests for the _resolve_cross_references function."""

    def _make_ruling(
        self,
        case_number: str | None = None,
        ruling_text: str | None = None,
        cross_reference_source: int | None = None,
    ) -> ExtractedRuling:
        """Helper to create an ExtractedRuling with minimal fields."""
        return ExtractedRuling(
            extracted_case_number=case_number,
            ruling_text=ruling_text,
            cross_reference_source=cross_reference_source,
        )

    def test_see_line_pattern(self) -> None:
        """'See Line N for tentative ruling' resolves to referenced entry."""
        long_text = "The motion is GRANTED. " * 10  # > 100 chars
        rulings = [
            self._make_ruling("2024-00001", long_text),
            self._make_ruling("2024-00002", "See Line 1 for tentative ruling."),
        ]
        entry_map = {1: 0, 2: 1}
        result = _resolve_cross_references(rulings, entry_map)
        assert result[1].ruling_text == long_text
        assert result[1].cross_reference_source == 1

    def test_scroll_down_to_line_pattern(self) -> None:
        """'scroll down to Line N' resolves to referenced entry."""
        long_text = "The demurrer is OVERRULED. " * 10
        rulings = [
            self._make_ruling("2024-00001", long_text),
            self._make_ruling("2024-00002", "scroll down to Line 1 for ruling"),
        ]
        entry_map = {1: 0, 2: 1}
        result = _resolve_cross_references(rulings, entry_map)
        assert result[1].ruling_text == long_text
        assert result[1].cross_reference_source == 1

    def test_scroll_down_to_lines_plural(self) -> None:
        """'Scroll down to Lines N' (plural) resolves to referenced entry."""
        long_text = "Detailed ruling text here. " * 10
        rulings = [
            self._make_ruling("2024-00001", long_text),
            self._make_ruling("2024-00002", "Scroll down to Lines 1"),
        ]
        entry_map = {1: 0, 2: 1}
        result = _resolve_cross_references(rulings, entry_map)
        assert result[1].ruling_text == long_text
        assert result[1].cross_reference_source == 1

    def test_ctrl_click_pattern(self) -> None:
        """'Ctrl Click here on Line N' resolves to referenced entry."""
        long_text = "The Court orders as follows: " * 10
        rulings = [
            self._make_ruling("2024-00001", long_text),
            self._make_ruling("2024-00002", "Ctrl Click here on Line 1"),
        ]
        entry_map = {1: 0, 2: 1}
        result = _resolve_cross_references(rulings, entry_map)
        assert result[1].ruling_text == long_text
        assert result[1].cross_reference_source == 1

    def test_click_on_line_pattern(self) -> None:
        """'Click on Line N' resolves to referenced entry."""
        long_text = "Motion for summary judgment is DENIED. " * 10
        rulings = [
            self._make_ruling("2024-00001", long_text),
            self._make_ruling("2024-00002", "Click on Line 1 for ruling"),
        ]
        entry_map = {1: 0, 2: 1}
        result = _resolve_cross_references(rulings, entry_map)
        assert result[1].ruling_text == long_text
        assert result[1].cross_reference_source == 1

    def test_court_addressed_pattern(self) -> None:
        """'[The Court addressed ... at Line N above]' resolves."""
        long_text = "The petition is GRANTED. " * 10
        rulings = [
            self._make_ruling("2024-00001", long_text),
            self._make_ruling(
                "2024-00002",
                "[The Court addressed this matter at Line 1 above]",
            ),
        ]
        entry_map = {1: 0, 2: 1}
        result = _resolve_cross_references(rulings, entry_map)
        assert result[1].ruling_text == long_text
        assert result[1].cross_reference_source == 1

    def test_order_above_uses_previous_entry(self) -> None:
        """'[Order above]' resolves to the immediately preceding entry."""
        long_text = "The motion to compel is GRANTED. " * 10
        rulings = [
            self._make_ruling("2024-00001", long_text),
            self._make_ruling("2024-00002", "[Order above]"),
        ]
        entry_map = {1: 0, 2: 1}
        result = _resolve_cross_references(rulings, entry_map)
        assert result[1].ruling_text == long_text
        assert result[1].cross_reference_source == 1

    def test_order_above_first_entry_no_crash(self) -> None:
        """'[Order above]' on the first entry doesn't crash (no previous)."""
        rulings = [
            self._make_ruling("2024-00001", "[Order above]"),
        ]
        entry_map = {1: 0}
        result = _resolve_cross_references(rulings, entry_map)
        # No previous entry, so text should remain unchanged.
        assert result[0].ruling_text == "[Order above]"
        assert result[0].cross_reference_source is None

    def test_no_resolution_for_very_long_stub_text(self) -> None:
        """Entries with very long ruling_text (> 2000 chars) are not resolved (#2416)."""
        # A ruling with real content that happens to mention another line in
        # passing — we must not overwrite it with the referenced text.  The
        # stub cap protects against this.
        long_stub = "The motion is DENIED. " * 120 + " See Line 1 for related matter."
        assert len(long_stub) > 2000
        ref_text = "The motion is GRANTED. " * 10
        rulings = [
            self._make_ruling("2024-00001", ref_text),
            self._make_ruling("2024-00002", long_stub),
        ]
        entry_map = {1: 0, 2: 1}
        result = _resolve_cross_references(rulings, entry_map)
        # Second entry is longer than the cap, so should NOT be resolved.
        assert result[1].ruling_text == long_stub
        assert result[1].cross_reference_source is None

    def test_no_resolution_when_stub_longer_than_ref(self) -> None:
        """Stub text that is already longer than the referenced entry is not overwritten (#2416)."""
        stub_text = (
            "The motion is DENIED with prejudice. "
            "The Court finds the moving party has failed to establish good cause. "
            "See Line 1 for additional context."
        )
        # Referenced text is SHORTER than the stub — we should not overwrite
        # the longer stub with a shorter reference.
        short_ref = "GRANTED in part, DENIED in part."
        assert len(short_ref) < len(stub_text)
        rulings = [
            self._make_ruling("2024-00001", short_ref),
            self._make_ruling("2024-00002", stub_text),
        ]
        entry_map = {1: 0, 2: 1}
        result = _resolve_cross_references(rulings, entry_map)
        assert result[1].ruling_text == stub_text
        assert result[1].cross_reference_source is None

    def test_long_stub_with_header_prefix_resolves(self) -> None:
        """Santa Clara stubs with caption-header prefix + 'See Line N' resolve (#2416).

        Santa Clara PDFs often include a bold caption header before the
        cross-reference phrase, e.g. "**Order on Defendants' Demurrer to the
        Fifth Cause of Action and Motion to Strike**\\n\\nSee Line 1 below for
        complete tentative ruling..." which typically runs 300-500 chars.
        The old threshold of 100 chars caused these to be silently skipped.
        """
        long_stub = (
            "**Order on Defendants' Demurrer to the Fifth Cause of Action "
            "and Motion to Strike**\n\n"
            "See Line 1 below for complete tentative ruling. After the "
            "hearing, the Court will prepare and file one formal Order "
            "addressing both Defendants' Demurrer and Motion to Strike."
        )
        # Sanity: the stub is longer than the old 100-char threshold but
        # shorter than the new 2000-char cap.
        assert 100 < len(long_stub) < 2000
        long_ref = "The demurrer is OVERRULED. " * 30
        rulings = [
            self._make_ruling("2024-00001", long_ref),
            self._make_ruling("2024-00002", long_stub),
        ]
        entry_map = {1: 0, 2: 1}
        result = _resolve_cross_references(rulings, entry_map)
        assert result[1].ruling_text == long_ref
        assert result[1].cross_reference_source == 1

    def test_no_resolution_when_ref_not_found(self) -> None:
        """Cross-reference to non-existent line number leaves text unchanged."""
        rulings = [
            self._make_ruling("2024-00001", "See Line 99 for tentative ruling."),
        ]
        entry_map = {1: 0}
        result = _resolve_cross_references(rulings, entry_map)
        assert result[0].ruling_text == "See Line 99 for tentative ruling."
        assert result[0].cross_reference_source is None

    def test_no_resolution_when_ref_text_too_short(self) -> None:
        """Cross-reference to entry with short ruling_text leaves text unchanged."""
        rulings = [
            self._make_ruling("2024-00001", "GRANTED."),
            self._make_ruling("2024-00002", "See Line 1 for tentative ruling."),
        ]
        entry_map = {1: 0, 2: 1}
        result = _resolve_cross_references(rulings, entry_map)
        # Referenced entry text is too short (< 100 chars), so no resolution.
        assert result[1].ruling_text == "See Line 1 for tentative ruling."
        assert result[1].cross_reference_source is None

    def test_multiple_cross_references(self) -> None:
        """Multiple entries referencing the same line all get resolved."""
        long_text = "The motion is GRANTED. " * 10
        rulings = [
            self._make_ruling("2024-00001", long_text),
            self._make_ruling("2024-00002", "See Line 1 for tentative ruling."),
            self._make_ruling("2024-00003", "See Line 1 for tentative ruling."),
        ]
        entry_map = {1: 0, 2: 1, 3: 2}
        result = _resolve_cross_references(rulings, entry_map)
        assert result[1].ruling_text == long_text
        assert result[1].cross_reference_source == 1
        assert result[2].ruling_text == long_text
        assert result[2].cross_reference_source == 1

    def test_no_xref_plain_text(self) -> None:
        """Short text that is NOT a cross-reference is left alone."""
        rulings = [
            self._make_ruling("2024-00001", "GRANTED."),
        ]
        entry_map = {1: 0}
        result = _resolve_cross_references(rulings, entry_map)
        assert result[0].ruling_text == "GRANTED."
        assert result[0].cross_reference_source is None

    def test_none_ruling_text_skipped(self) -> None:
        """Entries with None ruling_text are skipped."""
        rulings = [
            self._make_ruling("2024-00001", None),
        ]
        entry_map = {1: 0}
        result = _resolve_cross_references(rulings, entry_map)
        assert result[0].ruling_text is None

    def test_self_reference_not_resolved(self) -> None:
        """Entry referencing itself does not create an infinite copy."""
        rulings = [
            self._make_ruling("2024-00001", "See Line 1 for tentative ruling."),
        ]
        entry_map = {1: 0}
        result = _resolve_cross_references(rulings, entry_map)
        assert result[0].ruling_text == "See Line 1 for tentative ruling."
        assert result[0].cross_reference_source is None


class TestCrossReferenceIntegration:
    """Integration tests: cross-reference resolution through _join_page_rows."""

    def test_see_line_resolved_via_join_page_rows(self) -> None:
        """Cross-reference entries are resolved when going through _join_page_rows."""
        long_text = ("The motion is GRANTED. The Court finds good cause. " * 5).strip()
        rows = [
            {
                "entry_number": 1,
                "case_info": "20CV374597 Regional Medical v. County of SC",
                "ruling_text": long_text,
            },
            {
                "entry_number": 2,
                "case_info": "20CV374597 Regional Medical v. County of SC",
                "ruling_text": "See Line 1 for tentative ruling.",
            },
            {
                "entry_number": 3,
                "case_info": "20CV374597 Regional Medical v. County of SC",
                "ruling_text": "See Line 1 for tentative ruling.",
            },
            {
                "entry_number": 4,
                "case_info": "21CV378378 Wong-Mikhail v. Sutter Bay",
                "ruling_text": "DENIED without prejudice.",
            },
        ]
        rulings = _join_page_rows(rows)
        assert len(rulings) == 4
        # Lines 2 and 3 should have Line 1's ruling text.
        assert rulings[1].ruling_text == long_text
        assert rulings[1].cross_reference_source == 1
        assert rulings[2].ruling_text == long_text
        assert rulings[2].cross_reference_source == 1
        # Line 1 should keep its own text.
        assert rulings[0].ruling_text == long_text
        assert rulings[0].cross_reference_source is None
        # Line 4 should be untouched.
        assert rulings[3].ruling_text == "DENIED without prejudice."
        assert rulings[3].cross_reference_source is None

    def test_order_above_resolved_via_join_page_rows(self) -> None:
        """'[Order above]' entries are resolved through _join_page_rows."""
        long_text = ("The petition is GRANTED with modifications. " * 5).strip()
        rows = [
            {
                "entry_number": 5,
                "case_info": "24PR196490 In re the Klein Trust",
                "ruling_text": long_text,
            },
            {
                "entry_number": 6,
                "case_info": "24PR196491 In re the Smith Trust",
                "ruling_text": "[Order above]",
            },
        ]
        rulings = _join_page_rows(rows)
        assert len(rulings) == 2
        assert rulings[1].ruling_text == long_text
        assert rulings[1].cross_reference_source == 5

    def test_xref_not_deduped(self) -> None:
        """Cross-referenced ruling text is NOT nulled by deduplication."""
        long_text = ("The motion for summary adjudication is DENIED. " * 10).strip()
        rows = [
            {
                "entry_number": 1,
                "case_info": "20CV374597 Regional Medical v. County",
                "ruling_text": long_text,
            },
            {
                "entry_number": 2,
                "case_info": "20CV374598 Other Medical v. County",
                "ruling_text": "See Line 1 for tentative ruling.",
            },
        ]
        rulings = _join_page_rows(rows)
        assert len(rulings) == 2
        # Both should have the ruling text — dedup should NOT null the xref.
        assert rulings[0].ruling_text == long_text
        assert rulings[1].ruling_text == long_text
        assert rulings[1].cross_reference_source == 1

    def test_existing_rulings_not_affected(self) -> None:
        """Existing rulings with real text are not affected by xref resolution."""
        rows = [
            {
                "entry_number": 1,
                "case_info": "2024-00001 Smith v. Jones",
                "ruling_text": "The motion is GRANTED.",
            },
            {
                "entry_number": 2,
                "case_info": "2024-00002 Alpha v. Beta",
                "ruling_text": "The demurrer is OVERRULED.",
            },
        ]
        rulings = _join_page_rows(rows)
        assert len(rulings) == 2
        assert rulings[0].ruling_text == "The motion is GRANTED."
        assert rulings[0].cross_reference_source is None
        assert rulings[1].ruling_text == "The demurrer is OVERRULED."
        assert rulings[1].cross_reference_source is None

    def test_cross_page_xref_resolves_after_join(self) -> None:
        """Cross-references spanning page boundaries resolve via _join_page_rows (#2416).

        The multimodal extractor processes each page independently but
        concatenates the rows from all pages into a single list before
        calling ``_join_page_rows``.  This test simulates that flow: line 1's
        full text is on page 1, while the line 4 stub lives on page 2.
        After ``_join_page_rows``, the stub should be resolved against
        line 1's text — the joined ``entry_number_to_index`` is built from
        every page's rows.
        """
        long_text = ("The motion is GRANTED in full. " * 8).strip()

        # Simulate per-page extraction: page 1 contains lines 1-3, page 2
        # contains lines 4-5.  The extractor .extend()s page_rows into a
        # single all_rows list (see LlmExtractor.extract_from_pdf), which is
        # exactly what we pre-build below.
        page1_rows = [
            {
                "entry_number": 1,
                "case_info": "20CV374597 Regional Medical v. County of SC",
                "ruling_text": long_text,
            },
            {
                "entry_number": 2,
                "case_info": "20CV374598 Other Medical v. County",
                "ruling_text": "See Line 1 for tentative ruling.",
            },
            {
                "entry_number": 3,
                "case_info": "21CV378378 Wong-Mikhail v. Sutter Bay",
                "ruling_text": "DENIED without prejudice. Parties may refile.",
            },
        ]
        page2_rows = [
            {
                "entry_number": 4,
                "case_info": "22CV400123 Third Party v. County",
                "ruling_text": "See Line 1 for tentative ruling.",
            },
            {
                "entry_number": 5,
                "case_info": "22CV400124 Fourth Party v. County",
                "ruling_text": "[Order above]",
            },
        ]
        all_rows = page1_rows + page2_rows

        rulings = _join_page_rows(all_rows)

        assert len(rulings) == 5
        # Line 1 keeps its own text.
        assert rulings[0].ruling_text == long_text
        assert rulings[0].cross_reference_source is None
        # Line 2 stub resolves to line 1 text (same page).
        assert rulings[1].ruling_text == long_text
        assert rulings[1].cross_reference_source == 1
        # Line 3 has its own text — untouched.
        assert rulings[2].ruling_text == "DENIED without prejudice. Parties may refile."
        assert rulings[2].cross_reference_source is None
        # Line 4 stub resolves to line 1 text — cross-page resolution.
        assert rulings[3].ruling_text == long_text
        assert rulings[3].cross_reference_source == 1
        # Line 5 [Order above] resolves to the preceding entry (line 4,
        # which itself was resolved to line 1's text).
        assert rulings[4].ruling_text == long_text
        # [Order above] uses the previous entry's entry_number.
        assert rulings[4].cross_reference_source == 4
