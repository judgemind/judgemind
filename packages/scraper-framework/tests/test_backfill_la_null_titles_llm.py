"""Tests for the backfill_la_null_titles_llm script (#2006).

All database and LLM access is mocked — these tests verify the parsing,
validation, and update logic without requiring live services.
"""

from __future__ import annotations

import importlib
import os
import sys
from unittest.mock import MagicMock, patch

_SCRIPTS_DIR = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "..",
    "scripts",
)
sys.path.insert(0, _SCRIPTS_DIR)
backfill = importlib.import_module("backfill_la_null_titles_llm")


# ---------------------------------------------------------------------------
# parse_llm_title unit tests
# ---------------------------------------------------------------------------


class TestParseLlmTitle:
    """Tests for the parse_llm_title() function."""

    def test_valid_json_response(self) -> None:
        response = '{"case_title": "Smith v. Jones"}'
        assert backfill.parse_llm_title(response) == "Smith v. Jones"

    def test_json_with_code_fences(self) -> None:
        response = '```json\n{"case_title": "Smith v. Jones"}\n```'
        assert backfill.parse_llm_title(response) == "Smith v. Jones"

    def test_null_case_title(self) -> None:
        response = '{"case_title": null}'
        assert backfill.parse_llm_title(response) is None

    def test_empty_case_title(self) -> None:
        response = '{"case_title": ""}'
        assert backfill.parse_llm_title(response) is None

    def test_too_short_title(self) -> None:
        response = '{"case_title": "A v"}'
        assert backfill.parse_llm_title(response) is None

    def test_too_long_title(self) -> None:
        long_name = "A" * 200
        response = f'{{"case_title": "{long_name} v. Jones"}}'
        assert backfill.parse_llm_title(response) is None

    def test_rejects_procedural_text(self) -> None:
        response = '{"case_title": "Motion to Compel v. Demurrer"}'
        assert backfill.parse_llm_title(response) is None

    def test_rejects_invalid_json(self) -> None:
        response = "not valid json"
        assert backfill.parse_llm_title(response) is None

    def test_rejects_non_dict(self) -> None:
        response = '["Smith v. Jones"]'
        assert backfill.parse_llm_title(response) is None

    def test_rejects_non_string_title(self) -> None:
        response = '{"case_title": 42}'
        assert backfill.parse_llm_title(response) is None

    def test_strips_whitespace(self) -> None:
        response = '{"case_title": "  Smith v. Jones  "}'
        assert backfill.parse_llm_title(response) == "Smith v. Jones"

    def test_title_with_et_al(self) -> None:
        response = '{"case_title": "Smith, et al. v. Jones Corp."}'
        assert backfill.parse_llm_title(response) == "Smith, et al. v. Jones Corp."


# ---------------------------------------------------------------------------
# extract_title_via_llm tests (mocked LLM)
# ---------------------------------------------------------------------------


class TestExtractTitleViaLlm:
    """Tests for the extract_title_via_llm() function with mocked LLM."""

    @patch("backfill_la_null_titles_llm.call_llm")
    def test_successful_extraction(self, mock_call: MagicMock) -> None:
        mock_response = MagicMock()
        mock_response.text = '{"case_title": "Buenaventura v. City of Pasadena"}'
        mock_call.return_value = mock_response

        result = backfill.extract_title_via_llm("Some ruling text here...")

        assert result == "Buenaventura v. City of Pasadena"
        mock_call.assert_called_once()

    @patch("backfill_la_null_titles_llm.call_llm")
    def test_llm_returns_none(self, mock_call: MagicMock) -> None:
        mock_call.return_value = None

        result = backfill.extract_title_via_llm("Some ruling text...")

        assert result is None

    @patch("backfill_la_null_titles_llm.call_llm")
    def test_llm_cannot_determine_title(self, mock_call: MagicMock) -> None:
        mock_response = MagicMock()
        mock_response.text = '{"case_title": null}'
        mock_call.return_value = mock_response

        result = backfill.extract_title_via_llm("Ruling with no party names...")

        assert result is None

    @patch("backfill_la_null_titles_llm.call_llm")
    def test_truncates_long_text(self, mock_call: MagicMock) -> None:
        mock_response = MagicMock()
        mock_response.text = '{"case_title": "Smith v. Jones"}'
        mock_call.return_value = mock_response

        long_text = "x" * 10000
        backfill.extract_title_via_llm(long_text)

        # Verify the user_message passed to call_llm is truncated
        call_kwargs = mock_call.call_args
        user_msg = call_kwargs.kwargs.get("user_message") or call_kwargs[1].get("user_message")
        if user_msg is None:
            # Positional argument
            user_msg = call_kwargs[0][1] if len(call_kwargs[0]) > 1 else None
        # The function truncates to 4000 chars
        assert user_msg is not None
        assert len(user_msg) == 4000


# ---------------------------------------------------------------------------
# fetch_null_title_cases tests (mocked DB)
# ---------------------------------------------------------------------------


class TestFetchNullTitleCases:
    """Tests for the fetch_null_title_cases() function."""

    def test_returns_cases_from_db(self) -> None:
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        cur.fetchall.return_value = [
            ("uuid-1", "24STCV12345", "Some ruling text..."),
            ("uuid-2", "24STCV67890", "Another ruling text..."),
        ]

        result = backfill.fetch_null_title_cases(conn)

        assert len(result) == 2
        assert result[0]["case_id"] == "uuid-1"
        assert result[0]["case_number"] == "24STCV12345"
        assert result[1]["case_id"] == "uuid-2"

    def test_returns_empty_list_when_no_cases(self) -> None:
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        cur.fetchall.return_value = []

        result = backfill.fetch_null_title_cases(conn)

        assert result == []


# ---------------------------------------------------------------------------
# update_case_title tests (mocked DB)
# ---------------------------------------------------------------------------


class TestUpdateCaseTitle:
    """Tests for the update_case_title() function."""

    def test_executes_update_query(self) -> None:
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        backfill.update_case_title(conn, "uuid-1", "Smith v. Jones")

        cur.execute.assert_called_once()
        params = cur.execute.call_args[0][1]
        assert params == ("Smith v. Jones", "uuid-1")
