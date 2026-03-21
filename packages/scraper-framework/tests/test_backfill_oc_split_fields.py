"""Tests for the OC split fields backfill script.

Tests cover:
  - clean_case_title: stripping ruling/motion text from garbled titles
  - extract_case_type_from_number: OC case number -> case type mapping
  - extract_parties_from_title: extracting plaintiff/defendant from case titles
  - run_backfill: savepoint behavior on party upsert failures
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Add scripts/ to sys.path so we can import the backfill module.
_SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "scripts")
sys.path.insert(0, os.path.normpath(_SCRIPTS_DIR))

from backfill_oc_split_fields import (  # noqa: E402
    clean_case_title,
    extract_case_type_from_number,
    extract_parties_from_title,
    run_backfill,
)

# ---------------------------------------------------------------------------
# clean_case_title tests
# ---------------------------------------------------------------------------


class TestCleanCaseTitle:
    """Test garbled title cleanup."""

    @pytest.mark.parametrize(
        "raw,expected_absent",
        [
            (
                "Gonzalez vs. Attorney\u2019s Fees in the Sum of $1,038,085.00 is GRANTED",
                ["GRANTED"],
            ),
            (
                "Jones vs. Saddle Creek of Class Action and PAGA Settlement is GRANTED IN",
                ["GRANTED", "Class Action"],
            ),
            (
                "Miranda vs. Chiron, Strike is DENIED.",
                ["DENIED"],
            ),
            (
                "Hallum vs. Restaurant to Compel Arbitration is GRANTED. IT IS ORDERED",
                ["GRANTED", "Compel"],
            ),
            (
                "vs. Kelley for Attorney Fees is GRANTED in the reduced amount of $67,746.32.",
                ["GRANTED", "Attorney Fees"],
            ),
            (
                "Robles vs. Bally Plaintiff\u2019s",
                ["Plaintiff"],
            ),
            (
                "Keller vs. System Defendant\u2019s",
                ["Defendant"],
            ),
            (
                "One LLP vs. CONTINUED TO APRIL 21, 2026, AT 9:00 A.M., IN",
                ["APRIL"],
            ),
        ],
    )
    def test_strips_junk(self, raw: str, expected_absent: list[str]) -> None:
        result = clean_case_title(raw)
        for word in expected_absent:
            assert word not in result, f"Expected '{word}' to be absent from '{result}'"

    def test_preserves_clean_title(self) -> None:
        title = "Gomez vs. Black Lion Farms, LLC"
        assert clean_case_title(title) == "Gomez vs. Black Lion Farms, LLC"

    def test_preserves_simple_title(self) -> None:
        title = "Smith vs. Jones"
        assert clean_case_title(title) == "Smith vs. Jones"

    def test_empty_string(self) -> None:
        assert clean_case_title("") == ""


# ---------------------------------------------------------------------------
# extract_case_type_from_number tests
# ---------------------------------------------------------------------------


class TestExtractCaseTypeFromNumber:
    """Test OC case number -> case type mapping."""

    @pytest.mark.parametrize(
        "case_number,expected",
        [
            ("30-2024-01393434", "civil"),
            ("2024-01380242", "civil"),
            ("30-2018-00970921", "civil"),
            ("25-01451474", "civil"),
        ],
    )
    def test_oc_civil_numbers(self, case_number: str, expected: str) -> None:
        assert extract_case_type_from_number(case_number) == expected

    def test_none_input(self) -> None:
        assert extract_case_type_from_number(None) is None  # type: ignore[arg-type]

    def test_empty_string(self) -> None:
        assert extract_case_type_from_number("") is None

    def test_unknown_format(self) -> None:
        assert extract_case_type_from_number("ABC123") is None


# ---------------------------------------------------------------------------
# extract_parties_from_title tests
# ---------------------------------------------------------------------------


class TestExtractPartiesFromTitle:
    """Test party extraction from case titles."""

    def test_basic_vs(self) -> None:
        parties = extract_parties_from_title("Smith vs. Jones")
        assert len(parties) == 2
        assert parties[0]["name"] == "Smith"
        assert parties[0]["role"] == "plaintiff"
        assert parties[1]["name"] == "Jones"
        assert parties[1]["role"] == "defendant"

    def test_vs_without_period(self) -> None:
        parties = extract_parties_from_title("Smith vs Jones")
        assert len(parties) == 2

    def test_v_with_period(self) -> None:
        parties = extract_parties_from_title("Smith v. Jones")
        assert len(parties) == 2

    def test_company_name(self) -> None:
        parties = extract_parties_from_title("Gomez vs. Black Lion Farms, LLC")
        assert len(parties) == 2
        assert "Black Lion Farms, Llc" in parties[1]["name"]

    def test_empty_title(self) -> None:
        assert extract_parties_from_title("") == []

    def test_no_vs(self) -> None:
        assert extract_parties_from_title("Some Random Title") == []

    def test_title_case_normalization(self) -> None:
        parties = extract_parties_from_title("SMITH vs. JONES")
        assert parties[0]["name"] == "Smith"
        assert parties[1]["name"] == "Jones"


# ---------------------------------------------------------------------------
# run_backfill savepoint tests
# ---------------------------------------------------------------------------


class TestRunBackfillSavepoints:
    """Test that party upsert failures use savepoints, not full rollback."""

    def _make_mock_conn(
        self,
        rows: list[tuple[str, str, str, str | None, bool]],
        *,
        fail_party_name: str | None = None,
    ) -> MagicMock:
        """Create a mock psycopg connection with cursor support.

        Args:
            rows: List of (case_id, case_title, case_number, case_type, has_parties)
                  tuples returned by the FETCH_CASES_QUERY.
            fail_party_name: If set, _upsert_party_and_link will raise when
                             this party name is encountered.
        """
        conn = MagicMock()

        # Make conn usable as a context manager (with psycopg.connect(...) as conn:)
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)

        # Cursor for the initial fetch query
        fetch_cursor = MagicMock()
        fetch_cursor.fetchall.return_value = rows
        fetch_cursor.__enter__ = MagicMock(return_value=fetch_cursor)
        fetch_cursor.__exit__ = MagicMock(return_value=False)

        # Cursor for UPDATE statements
        update_cursor = MagicMock()
        update_cursor.__enter__ = MagicMock(return_value=update_cursor)
        update_cursor.__exit__ = MagicMock(return_value=False)

        # Return different cursors for sequential calls
        conn.cursor.side_effect = [fetch_cursor, update_cursor] + [
            MagicMock(
                __enter__=MagicMock(return_value=MagicMock()),
                __exit__=MagicMock(return_value=False),
            )
            for _ in range(20)
        ]

        # Track execute calls for savepoint verification
        conn.execute = MagicMock()

        return conn

    @patch("backfill_oc_split_fields.psycopg")
    @patch("backfill_oc_split_fields._upsert_party_and_link")
    def test_party_failure_uses_savepoint_not_rollback(
        self,
        mock_upsert: MagicMock,
        mock_psycopg: MagicMock,
    ) -> None:
        """A party upsert failure should rollback to savepoint, not the whole tx.

        Scenario: case has a garbled title that gets cleaned AND has parties to
        extract. The title UPDATE should survive even if the party upsert fails.
        """
        # One case: garbled title with parties, no case_type, no existing parties
        rows = [
            (
                "case-uuid-1",
                "Smith vs. Jones Motion for Summary Judgment is GRANTED",
                "30-2024-01393434",
                None,  # no case_type
                False,  # no existing parties
            ),
        ]

        conn = self._make_mock_conn(rows)
        mock_psycopg.connect.return_value = conn

        # Make _upsert_party_and_link fail for the first party
        mock_upsert.side_effect = Exception("unique constraint violation")

        run_backfill("fake://dsn", dry_run=False)

        # Verify savepoint was used (not conn.rollback)
        execute_calls = [str(c) for c in conn.execute.call_args_list]
        savepoint_calls = [c for c in execute_calls if "SAVEPOINT" in c]
        assert len(savepoint_calls) > 0, "Expected SAVEPOINT calls but found none"

        # Verify ROLLBACK TO SAVEPOINT was called (not bare rollback)
        rollback_to_calls = [c for c in execute_calls if "ROLLBACK TO SAVEPOINT" in c]
        assert len(rollback_to_calls) > 0, "Expected ROLLBACK TO SAVEPOINT but found none"

        # Verify conn.rollback() was NOT called (the old buggy behavior)
        conn.rollback.assert_not_called()

        # Verify conn.commit() was still called (title/case_type update survived)
        conn.commit.assert_called()

    @patch("backfill_oc_split_fields.psycopg")
    @patch("backfill_oc_split_fields._upsert_party_and_link")
    def test_successful_party_upsert_releases_savepoint(
        self,
        mock_upsert: MagicMock,
        mock_psycopg: MagicMock,
    ) -> None:
        """Successful party upserts should RELEASE the savepoint."""
        rows = [
            (
                "case-uuid-1",
                "Smith vs. Jones",
                "30-2024-01393434",
                None,
                False,
            ),
        ]

        conn = self._make_mock_conn(rows)
        mock_psycopg.connect.return_value = conn
        mock_upsert.return_value = None  # success

        run_backfill("fake://dsn", dry_run=False)

        execute_calls = [str(c) for c in conn.execute.call_args_list]
        release_calls = [c for c in execute_calls if "RELEASE SAVEPOINT" in c]
        assert len(release_calls) > 0, "Expected RELEASE SAVEPOINT but found none"

        # No rollback calls at all
        conn.rollback.assert_not_called()
        rollback_to_calls = [c for c in execute_calls if "ROLLBACK TO SAVEPOINT" in c]
        assert len(rollback_to_calls) == 0, "Did not expect ROLLBACK TO SAVEPOINT on success"

    @patch("backfill_oc_split_fields.psycopg")
    @patch("backfill_oc_split_fields._upsert_party_and_link")
    def test_party_failure_does_not_discard_title_update(
        self,
        mock_upsert: MagicMock,
        mock_psycopg: MagicMock,
    ) -> None:
        """Title update should survive even when party upsert fails.

        This is the core acceptance criterion: a party upsert failure must not
        discard unrelated case title/case_type updates in the same batch.
        """
        rows = [
            # Case 1: title needs cleaning, has parties -> party upsert will fail
            (
                "case-uuid-1",
                "Alpha vs. Beta Motion for Summary Judgment is GRANTED",
                "30-2024-00000001",
                None,
                False,
            ),
            # Case 2: title needs cleaning, no parties to extract
            (
                "case-uuid-2",
                "Some Garbled Title GRANTED in part",
                "30-2024-00000002",
                None,
                True,  # already has parties
            ),
        ]

        conn = self._make_mock_conn(rows)
        mock_psycopg.connect.return_value = conn
        mock_upsert.side_effect = Exception("party insert failed")

        stats = run_backfill("fake://dsn", dry_run=False)

        # Both titles should have been cleaned (tracked in stats)
        assert stats["titles_cleaned"] == 2

        # conn.commit() should have been called (batch not discarded)
        conn.commit.assert_called()

        # conn.rollback() should NOT have been called
        conn.rollback.assert_not_called()
