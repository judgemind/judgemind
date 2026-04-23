"""Tests for the backfill_oc_null_titles_llm script (#2637).

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
backfill = importlib.import_module("backfill_oc_null_titles_llm")


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
        response = '{"case_title": "  Nguyen v. City of Anaheim  "}'
        assert backfill.parse_llm_title(response) == "Nguyen v. City of Anaheim"

    def test_title_with_et_al(self) -> None:
        response = '{"case_title": "Nguyen, et al. v. Orange County Sheriff"}'
        assert backfill.parse_llm_title(response) == "Nguyen, et al. v. Orange County Sheriff"

    def test_vs_normalization_not_applied(self) -> None:
        """parse_llm_title does not mutate 'vs.' — it validates the LLM output."""
        # The LLM is instructed to use 'v.' but parse_llm_title itself does
        # not transform 'vs.' into 'v.' — it returns the string as-is if it
        # passes all other checks.
        response = '{"case_title": "Smith vs. Jones Corp"}'
        result = backfill.parse_llm_title(response)
        # 13 chars, no procedural words — should pass length/procedural checks
        assert result == "Smith vs. Jones Corp"


# ---------------------------------------------------------------------------
# extract_title_via_llm tests (mocked LLM)
# ---------------------------------------------------------------------------


class TestExtractTitleViaLlm:
    """Tests for the extract_title_via_llm() function with mocked LLM."""

    @patch("backfill_oc_null_titles_llm.call_llm")
    def test_successful_extraction(self, mock_call: MagicMock) -> None:
        mock_response = MagicMock()
        mock_response.text = '{"case_title": "Tran v. City of Irvine"}'
        mock_call.return_value = mock_response

        result = backfill.extract_title_via_llm("Some ruling text here...")

        assert result == "Tran v. City of Irvine"
        mock_call.assert_called_once()

    @patch("backfill_oc_null_titles_llm.call_llm")
    def test_llm_returns_none(self, mock_call: MagicMock) -> None:
        mock_call.return_value = None

        result = backfill.extract_title_via_llm("Some ruling text...")

        assert result is None

    @patch("backfill_oc_null_titles_llm.call_llm")
    def test_llm_cannot_determine_title(self, mock_call: MagicMock) -> None:
        mock_response = MagicMock()
        mock_response.text = '{"case_title": null}'
        mock_call.return_value = mock_response

        result = backfill.extract_title_via_llm("Ruling with no party names...")

        assert result is None

    @patch("backfill_oc_null_titles_llm.call_llm")
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

    @patch("backfill_oc_null_titles_llm.call_llm")
    def test_uses_google_gemini_defaults(self, mock_call: MagicMock) -> None:
        """Default provider/model should be google/gemini-2.5-flash-lite."""
        mock_response = MagicMock()
        mock_response.text = '{"case_title": "Lee v. County of Orange"}'
        mock_call.return_value = mock_response

        backfill.extract_title_via_llm("Some ruling text.")

        call_kwargs = mock_call.call_args
        kwargs = call_kwargs.kwargs if call_kwargs.kwargs else call_kwargs[1]
        assert kwargs.get("provider") == "google"
        assert kwargs.get("model") == "gemini-2.5-flash-lite"


# ---------------------------------------------------------------------------
# fetch_null_title_cases tests (mocked DB) — OC-specific SQL filter
# ---------------------------------------------------------------------------


class TestFetchNullTitleCases:
    """Tests for the fetch_null_title_cases() function."""

    def test_returns_cases_from_db(self) -> None:
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        cur.fetchall.return_value = [
            ("uuid-1", "UNKNOWN-abc123", "Some ruling text..."),
            ("uuid-2", "UNKNOWN-def456", "Another ruling text..."),
        ]

        result = backfill.fetch_null_title_cases(conn)

        assert len(result) == 2
        assert result[0]["case_id"] == "uuid-1"
        assert result[0]["case_number"] == "UNKNOWN-abc123"
        assert result[1]["case_id"] == "uuid-2"

    def test_returns_empty_list_when_no_cases(self) -> None:
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        cur.fetchall.return_value = []

        result = backfill.fetch_null_title_cases(conn)

        assert result == []

    def test_sql_filters_orange_county(self) -> None:
        """Verify the SQL query filters for Orange County."""
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        cur.fetchall.return_value = []

        backfill.fetch_null_title_cases(conn)

        executed_query = cur.execute.call_args[0][0]
        assert "Orange" in executed_query
        assert "co.county" in executed_query

    def test_sql_filters_unknown_case_numbers(self) -> None:
        """Verify the SQL query filters for UNKNOWN-% case numbers."""
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        cur.fetchall.return_value = []

        backfill.fetch_null_title_cases(conn)

        executed_query = cur.execute.call_args[0][0]
        assert "UNKNOWN-%" in executed_query
        assert "ca.case_number LIKE" in executed_query

    def test_sql_filters_null_case_title(self) -> None:
        """Verify the SQL query filters for NULL case_title."""
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        cur.fetchall.return_value = []

        backfill.fetch_null_title_cases(conn)

        executed_query = cur.execute.call_args[0][0]
        assert "ca.case_title IS NULL" in executed_query

    def test_limit_applied_when_provided(self) -> None:
        """Verify a LIMIT clause is appended when limit is specified."""
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        cur.fetchall.return_value = []

        backfill.fetch_null_title_cases(conn, limit=10)

        executed_query = cur.execute.call_args[0][0]
        assert "LIMIT 10" in executed_query

    def test_no_limit_when_not_provided(self) -> None:
        """Verify no top-level LIMIT clause is appended when limit is None."""
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        cur.fetchall.return_value = []

        backfill.fetch_null_title_cases(conn)

        executed_query = cur.execute.call_args[0][0]
        # The query uses LATERAL (which also contains LIMIT) in the subquery;
        # check that there is no trailing LIMIT clause added by the function.
        # We verify by confirming "LIMIT {N}" pattern does not appear at end.
        import re

        assert not re.search(r"LIMIT\s+\d+\s*$", executed_query.strip())


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

        backfill.update_case_title(conn, "uuid-1", "Nguyen v. City of Anaheim")

        cur.execute.assert_called_once()
        params = cur.execute.call_args[0][1]
        assert params == ("Nguyen v. City of Anaheim", "uuid-1")

    def test_only_updates_null_titles(self) -> None:
        """Verify the UPDATE only touches rows where case_title IS NULL."""
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        backfill.update_case_title(conn, "uuid-2", "Lee v. County of Orange")

        executed_query = cur.execute.call_args[0][0]
        assert "case_title IS NULL" in executed_query


# ---------------------------------------------------------------------------
# main() integration tests (mocked psycopg + LLM)
# ---------------------------------------------------------------------------


class TestMain:
    """Integration tests for the main() entry point."""

    @patch("backfill_oc_null_titles_llm.psycopg")
    @patch("backfill_oc_null_titles_llm.call_llm")
    def test_dry_run_does_not_call_update(
        self, mock_call_llm: MagicMock, mock_psycopg: MagicMock
    ) -> None:
        """Dry-run should log proposed updates but not call update_case_title."""
        mock_conn = MagicMock()
        mock_psycopg.connect.return_value = mock_conn

        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_cur.fetchall.return_value = [
            (
                "uuid-1",
                "UNKNOWN-abc",
                "Plaintiff Nguyen moves to compel further discovery responses from "
                "defendant City of Anaheim. The court finds in favor of Plaintiff.",
            ),
        ]

        mock_response = MagicMock()
        mock_response.text = '{"case_title": "Nguyen v. City of Anaheim"}'
        mock_call_llm.return_value = mock_response

        import os

        env_backup = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = "postgresql://fake/db"
        try:
            backfill.main.__globals__["sys"].argv = [
                "backfill_oc_null_titles_llm.py",
                "--dry-run",
                "--limit",
                "1",
            ]
            # main() exits cleanly; no commit should be called
            backfill.main()
        finally:
            if env_backup is None:
                del os.environ["DATABASE_URL"]
            else:
                os.environ["DATABASE_URL"] = env_backup

        # In dry-run mode, commit should NOT have been called
        mock_conn.commit.assert_not_called()

    @patch("backfill_oc_null_titles_llm.psycopg")
    @patch("backfill_oc_null_titles_llm.call_llm")
    def test_write_path_commits(self, mock_call_llm: MagicMock, mock_psycopg: MagicMock) -> None:
        """Write path should call commit after each update."""
        mock_conn = MagicMock()
        mock_psycopg.connect.return_value = mock_conn

        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_cur.fetchall.return_value = [
            (
                "uuid-1",
                "UNKNOWN-abc",
                "Plaintiff Lee filed this lawsuit alleging breach of contract "
                "against defendant County of Orange. The court GRANTS the motion.",
            ),
        ]

        mock_response = MagicMock()
        mock_response.text = '{"case_title": "Lee v. County of Orange"}'
        mock_call_llm.return_value = mock_response

        import os

        env_backup = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = "postgresql://fake/db"
        try:
            backfill.main.__globals__["sys"].argv = [
                "backfill_oc_null_titles_llm.py",
                "--limit",
                "1",
            ]
            backfill.main()
        finally:
            if env_backup is None:
                del os.environ["DATABASE_URL"]
            else:
                os.environ["DATABASE_URL"] = env_backup

        # In write mode, commit should have been called
        mock_conn.commit.assert_called()

    @patch("backfill_oc_null_titles_llm.psycopg")
    def test_no_cases_exits_cleanly(self, mock_psycopg: MagicMock) -> None:
        """When no cases match the filter, main() should exit without error."""
        mock_conn = MagicMock()
        mock_psycopg.connect.return_value = mock_conn

        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_cur.fetchall.return_value = []

        import os

        env_backup = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = "postgresql://fake/db"
        try:
            backfill.main.__globals__["sys"].argv = [
                "backfill_oc_null_titles_llm.py",
                "--dry-run",
            ]
            backfill.main()  # Should not raise
        finally:
            if env_backup is None:
                del os.environ["DATABASE_URL"]
            else:
                os.environ["DATABASE_URL"] = env_backup

        mock_conn.commit.assert_not_called()
