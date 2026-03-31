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
from pathlib import Path
from unittest.mock import MagicMock, patch

import anthropic
import pytest

from framework.llm_extractor import (
    LlmExtractor,
    _create_google_client,
    _deduplicate_ruling_texts,
    _extract_case_number_from_info,
    _extract_case_title_from_info,
    _is_calendar_header,
    _is_new_case,
    _join_page_rows,
    _parse_page_rows,
    _render_pdf_pages,
)

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
