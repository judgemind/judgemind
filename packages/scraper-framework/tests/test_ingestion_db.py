"""Tests for ingestion/db.py.

Covers:
  - NUL byte stripping in text fields
  - Court, case, document, ruling, judge, and party upsert operations
  - Judge name normalization
  - Party name normalization and truncation
  - batch_upsert_parties function
  - Error handling (RuntimeError on missing rows)
"""

from __future__ import annotations

from datetime import date, datetime
from unittest.mock import MagicMock

import pytest

from ingestion.db import (
    _MAX_PARTY_NAME_LENGTH,
    _derive_court_code,
    _is_all_caps_title,
    _looks_like_valid_judge_name,
    _strip_middle_initials,
    _strip_nul,
    _truncate_party_name,
    batch_upsert_parties,
    delete_stale_split_children,
    insert_document,
    insert_document_and_ruling,
    insert_ruling,
    lookup_existing_case_title,
    normalize_case_title,
    normalize_judge_name,
    normalize_party_name,
    normalize_ruling_text_hash,
    resolve_judge,
    resolve_judge_from_department,
    upsert_case,
    upsert_case_judge,
    upsert_case_party,
    upsert_case_returning_title,
    upsert_court,
    upsert_party,
)

# ---------------------------------------------------------------------------
# _strip_nul helper
# ---------------------------------------------------------------------------


class TestStripNul:
    """Unit tests for the _strip_nul helper."""

    def test_removes_nul_bytes(self) -> None:
        assert _strip_nul("hello\x00world") == "helloworld"

    def test_removes_multiple_nul_bytes(self) -> None:
        assert _strip_nul("\x00a\x00b\x00c\x00") == "abc"

    def test_returns_none_for_none(self) -> None:
        assert _strip_nul(None) is None

    def test_passes_through_clean_string(self) -> None:
        assert _strip_nul("no nul bytes here") == "no nul bytes here"

    def test_returns_empty_string_for_only_nul(self) -> None:
        assert _strip_nul("\x00\x00\x00") == ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_conn() -> MagicMock:
    """Create a mock psycopg connection with cursor context manager."""
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    cur.fetchone.return_value = ("fake-uuid-1",)
    return conn


def _get_execute_args(conn: MagicMock) -> tuple:
    """Extract the parameter tuple from the last cursor.execute() call that has params.

    Skips calls without parameter tuples (e.g. SAVEPOINT, RELEASE SAVEPOINT).
    """
    cur = conn.cursor.return_value.__enter__.return_value
    # Iterate in reverse to find the last call with a params argument
    for call in reversed(cur.execute.call_args_list):
        if len(call[0]) > 1:
            return call[0][1]
    raise ValueError("No execute() call with params found")


# ---------------------------------------------------------------------------
# insert_ruling — NUL byte stripping
# ---------------------------------------------------------------------------


class TestInsertRulingNulStripping:
    """Verify insert_ruling strips NUL bytes from all text fields."""

    def test_ruling_text_nul_stripped(self) -> None:
        conn = _mock_conn()
        insert_ruling(
            conn,
            document_id="doc-1",
            case_id="case-1",
            court_id="court-1",
            hearing_date=date(2026, 3, 5),
            ruling_text="Motion\x00GRANTED.",
            department="Dept. 1",
        )
        args = _get_execute_args(conn)
        # ruling_text is at index 5 in the args tuple
        assert "\x00" not in str(args), "NUL byte found in execute args"

    def test_department_nul_stripped(self) -> None:
        conn = _mock_conn()
        insert_ruling(
            conn,
            document_id="doc-1",
            case_id="case-1",
            court_id="court-1",
            hearing_date=date(2026, 3, 5),
            ruling_text="Granted.",
            department="Dept\x00. 1",
        )
        args = _get_execute_args(conn)
        assert "\x00" not in str(args)

    def test_outcome_nul_stripped(self) -> None:
        conn = _mock_conn()
        insert_ruling(
            conn,
            document_id="doc-1",
            case_id="case-1",
            court_id="court-1",
            hearing_date=date(2026, 3, 5),
            ruling_text="Granted.",
            department="Dept. 1",
            outcome="granted\x00",
            motion_type="msj",
        )
        args = _get_execute_args(conn)
        assert "\x00" not in str(args)

    def test_motion_type_nul_stripped(self) -> None:
        conn = _mock_conn()
        insert_ruling(
            conn,
            document_id="doc-1",
            case_id="case-1",
            court_id="court-1",
            hearing_date=date(2026, 3, 5),
            ruling_text="Granted.",
            department="Dept. 1",
            motion_type="demur\x00rer",
        )
        args = _get_execute_args(conn)
        assert "\x00" not in str(args)

    def test_none_fields_stay_none(self) -> None:
        conn = _mock_conn()
        insert_ruling(
            conn,
            document_id="doc-1",
            case_id="case-1",
            court_id="court-1",
            hearing_date=date(2026, 3, 5),
            ruling_text=None,
            department=None,
            outcome=None,
            motion_type=None,
        )
        args = _get_execute_args(conn)
        # ruling_text (idx 5), department (idx 6), outcome (idx 7), motion_type (idx 8)
        assert args[5] is None
        assert args[6] is None
        assert args[7] is None
        assert args[8] is None


# ---------------------------------------------------------------------------
# upsert_case — NUL byte stripping
# ---------------------------------------------------------------------------


class TestUpsertCaseNulStripping:
    """Verify upsert_case strips NUL bytes from case_title."""

    def test_case_title_nul_stripped(self) -> None:
        conn = _mock_conn()
        upsert_case(
            conn,
            case_number="23STCV12345",
            court_id="court-1",
            case_title="Smith\x00 v. Jones",
        )
        args = _get_execute_args(conn)
        # case_title is the last arg (index 3)
        assert "\x00" not in str(args)

    def test_case_title_none_stays_none(self) -> None:
        conn = _mock_conn()
        upsert_case(
            conn,
            case_number="23STCV12345",
            court_id="court-1",
            case_title=None,
        )
        args = _get_execute_args(conn)
        assert args[3] is None


# ---------------------------------------------------------------------------
# upsert_party — NUL byte stripping
# ---------------------------------------------------------------------------


class TestUpsertPartyNulStripping:
    """Verify upsert_party strips NUL bytes from raw_name."""

    def test_raw_name_nul_stripped(self) -> None:
        conn = _mock_conn()
        # upsert_party first does a SELECT, so mock fetchone to return None
        # (no existing alias), then return a party_id for the INSERT
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.side_effect = [None, ("party-uuid-1",)]
        upsert_party(conn, raw_name="John\x00Doe", party_type="plaintiff")
        # Check that the INSERT calls don't contain NUL bytes
        for c in cur.execute.call_args_list:
            call_args = c[0][1]
            for arg in call_args:
                if isinstance(arg, str):
                    assert "\x00" not in arg, f"NUL byte found in arg: {arg!r}"


# ---------------------------------------------------------------------------
# batch_upsert_parties
# ---------------------------------------------------------------------------


def _mock_conn_for_batch() -> tuple[MagicMock, MagicMock]:
    """Create a mock connection suitable for batch_upsert_parties.

    Returns (conn, cur) where cur is the mock cursor.
    """
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return conn, cur


class TestBatchUpsertParties:
    """Tests for batch_upsert_parties()."""

    def test_empty_list_is_noop(self) -> None:
        """Empty parties_data should not issue any queries."""
        conn, cur = _mock_conn_for_batch()
        batch_upsert_parties(conn, "case-1", [])
        cur.execute.assert_not_called()
        cur.executemany.assert_not_called()

    def test_blank_names_filtered(self) -> None:
        """Parties with empty or whitespace names are skipped."""
        conn, cur = _mock_conn_for_batch()
        batch_upsert_parties(
            conn,
            "case-1",
            [
                {"name": "", "role": "plaintiff"},
                {"name": "   ", "role": "defendant"},
            ],
        )
        cur.execute.assert_not_called()
        cur.executemany.assert_not_called()

    def test_all_existing_parties_no_insert(self) -> None:
        """When all parties already exist, no INSERT into parties is needed."""
        conn, cur = _mock_conn_for_batch()
        # The SELECT returns both parties as already existing (lowercased keys)
        cur.fetchall.return_value = [
            ("john doe", "party-1"),
            ("jane smith", "party-2"),
        ]

        batch_upsert_parties(
            conn,
            "case-1",
            [
                {"name": "John Doe", "role": "plaintiff"},
                {"name": "Jane Smith", "role": "defendant"},
            ],
        )

        # Should have: 1 SELECT (batch lookup), 1 executemany (case_party links)
        assert cur.execute.call_count == 1  # SELECT only
        sql_select = cur.execute.call_args_list[0][0][0]
        assert "party_aliases" in sql_select
        assert "ANY" in sql_select

        # executemany for case_party links
        assert cur.executemany.call_count == 1
        executemany_sql = cur.executemany.call_args_list[0][0][0]
        assert "case_parties" in executemany_sql
        link_params = cur.executemany.call_args_list[0][0][1]
        assert len(link_params) == 2

    def test_new_parties_inserted(self) -> None:
        """When no existing aliases found, new parties and aliases are created."""
        conn, cur = _mock_conn_for_batch()
        # SELECT returns no existing aliases
        cur.fetchall.side_effect = [
            [],  # batch alias lookup
        ]
        # For executemany with returning=True, fetchone returns party IDs
        cur.fetchone.side_effect = [("pid-1",), ("pid-2",)]
        cur.nextset.side_effect = [True, False]

        batch_upsert_parties(
            conn,
            "case-1",
            [
                {"name": "John Doe", "role": "plaintiff"},
                {"name": "Jane Smith", "role": "defendant"},
            ],
        )

        # Should have: 1 SELECT, then executemany calls for:
        # parties INSERT, aliases INSERT, case_parties INSERT
        assert cur.execute.call_count == 1  # SELECT
        assert cur.executemany.call_count == 3  # parties, aliases, case_parties

        # Verify parties INSERT (executemany with returning=True)
        parties_call = cur.executemany.call_args_list[0]
        assert "INSERT INTO parties" in parties_call[0][0]
        assert parties_call[1].get("returning") is True
        assert len(parties_call[0][1]) == 2  # 2 new parties

        # Verify aliases INSERT
        aliases_call = cur.executemany.call_args_list[1]
        assert "party_aliases" in aliases_call[0][0]
        assert len(aliases_call[0][1]) == 2

        # Verify case_party links
        links_call = cur.executemany.call_args_list[2]
        assert "case_parties" in links_call[0][0]
        assert len(links_call[0][1]) == 2

    def test_mixed_existing_and_new(self) -> None:
        """Mix of existing and new parties only inserts the new ones."""
        conn, cur = _mock_conn_for_batch()
        # SELECT returns one existing alias (lowercased key from LOWER(raw_name))
        cur.fetchall.side_effect = [
            [("john doe", "existing-pid")],  # batch alias lookup
        ]
        # fetchone for the one new party
        cur.fetchone.side_effect = [("new-pid",)]
        cur.nextset.side_effect = [False]

        batch_upsert_parties(
            conn,
            "case-1",
            [
                {"name": "John Doe", "role": "plaintiff"},
                {"name": "Jane Smith", "role": "defendant"},
            ],
        )

        # parties INSERT should have 1 entry (only Jane Smith)
        parties_call = cur.executemany.call_args_list[0]
        assert len(parties_call[0][1]) == 1
        assert parties_call[0][1][0] == ("Jane Smith",)

    def test_nul_bytes_stripped_from_names(self) -> None:
        """NUL bytes in party names are stripped before processing."""
        conn, cur = _mock_conn_for_batch()
        cur.fetchall.side_effect = [[]]
        cur.fetchone.side_effect = [("pid-1",)]
        cur.nextset.side_effect = [False]

        batch_upsert_parties(
            conn,
            "case-1",
            [
                {"name": "John\x00Doe", "role": "plaintiff"},
            ],
        )

        # The SELECT should use the cleaned name
        select_args = cur.execute.call_args_list[0][0][1]
        assert "\x00" not in str(select_args)

        # The INSERT should use cleaned canonical name
        parties_params = cur.executemany.call_args_list[0][0][1]
        assert "\x00" not in str(parties_params)

    def test_deduplicates_by_raw_name(self) -> None:
        """Duplicate names (case-insensitive) are deduplicated."""
        conn, cur = _mock_conn_for_batch()
        cur.fetchall.side_effect = [[]]
        cur.fetchone.side_effect = [("pid-1",)]
        cur.nextset.side_effect = [False]

        batch_upsert_parties(
            conn,
            "case-1",
            [
                {"name": "John Doe", "role": "plaintiff"},
                {"name": "john doe", "role": "defendant"},  # duplicate
            ],
        )

        # Only 1 party should be inserted (deduped)
        parties_params = cur.executemany.call_args_list[0][0][1]
        assert len(parties_params) == 1

    def test_parties_without_role_skip_case_party_link(self) -> None:
        """Parties with empty role are upserted but not linked to the case."""
        conn, cur = _mock_conn_for_batch()
        cur.fetchall.side_effect = [[]]
        cur.fetchone.side_effect = [("pid-1",)]
        cur.nextset.side_effect = [False]

        batch_upsert_parties(
            conn,
            "case-1",
            [
                {"name": "John Doe", "role": ""},
            ],
        )

        # Should have SELECT + parties INSERT + aliases INSERT, but NO case_parties
        assert cur.execute.call_count == 1  # SELECT
        assert cur.executemany.call_count == 2  # parties + aliases (no case_parties)

    def test_custom_alias_source(self) -> None:
        """The alias_source parameter is passed to the aliases INSERT."""
        conn, cur = _mock_conn_for_batch()
        cur.fetchall.side_effect = [[]]
        cur.fetchone.side_effect = [("pid-1",)]
        cur.nextset.side_effect = [False]

        batch_upsert_parties(
            conn,
            "case-1",
            [{"name": "John Doe", "role": "plaintiff"}],
            alias_source="backfill",
        )

        aliases_params = cur.executemany.call_args_list[1][0][1]
        assert aliases_params[0][2] == "backfill"

    def test_case_insensitive_alias_lookup(self) -> None:
        """Verify batch alias lookup is case-insensitive (#1426).

        When a party alias exists as "John Doe" and we ingest "JOHN DOE",
        the existing party should be reused instead of creating a new one.
        """
        conn, cur = _mock_conn_for_batch()
        # Alias exists for lowercase "john doe"
        cur.fetchall.return_value = [("john doe", "existing-pid")]

        batch_upsert_parties(
            conn,
            "case-1",
            [{"name": "JOHN DOE", "role": "plaintiff"}],
        )

        # Should NOT insert any new parties — only the SELECT + case_party link
        assert cur.execute.call_count == 1  # SELECT only
        assert cur.executemany.call_count == 1  # case_parties link only

        # The SELECT query should use LOWER() for case-insensitive matching
        sql_select = cur.execute.call_args_list[0][0][0]
        assert "LOWER" in sql_select

        # The lowercased name list should be passed as the parameter
        select_params = cur.execute.call_args_list[0][0][1]
        assert select_params == (["john doe"],)

    def test_case_party_links_use_on_conflict(self) -> None:
        """Verify case_parties INSERT uses ON CONFLICT DO NOTHING (#873)."""
        conn, cur = _mock_conn_for_batch()
        cur.fetchall.return_value = [("john doe", "party-1")]

        batch_upsert_parties(
            conn,
            "case-1",
            [{"name": "John Doe", "role": "plaintiff"}],
        )

        # The last executemany call should be the case_parties INSERT
        links_call = cur.executemany.call_args_list[-1]
        sql = links_call[0][0]
        assert "case_parties" in sql
        assert "ON CONFLICT DO NOTHING" in sql

    def test_long_party_name_filtered_as_contaminated(self) -> None:
        """Party names exceeding _MAX_PARTY_NAME_LENGTH are truncated, then
        filtered out by the contamination check (names > 150 chars are not
        legitimate party names).  See #1932."""
        conn, cur = _mock_conn_for_batch()

        long_name = "A" * 9000
        batch_upsert_parties(
            conn,
            "case-1",
            [{"name": long_name, "role": "plaintiff"}],
        )

        # The contaminated name should be filtered — no DB calls at all
        cur.execute.assert_not_called()


# ---------------------------------------------------------------------------
# _truncate_party_name
# ---------------------------------------------------------------------------


class TestTruncatePartyName:
    """Tests for the _truncate_party_name helper."""

    def test_short_name_unchanged(self) -> None:
        assert _truncate_party_name("John Doe") == "John Doe"

    def test_exact_limit_unchanged(self) -> None:
        name = "A" * _MAX_PARTY_NAME_LENGTH
        assert _truncate_party_name(name) == name

    def test_over_limit_truncated(self) -> None:
        name = "B" * (_MAX_PARTY_NAME_LENGTH + 500)
        result = _truncate_party_name(name)
        assert len(result) == _MAX_PARTY_NAME_LENGTH

    def test_way_over_limit_truncated(self) -> None:
        """Reproduces the original bug: 9568-byte garbage party name."""
        name = "X" * 9568
        result = _truncate_party_name(name)
        assert len(result) == _MAX_PARTY_NAME_LENGTH


# ---------------------------------------------------------------------------
# upsert_party — truncation
# ---------------------------------------------------------------------------


class TestUpsertPartyTruncation:
    """Verify upsert_party truncates oversized party names."""

    def test_long_name_filtered_as_contaminated(self) -> None:
        """Very long party names are truncated, then rejected by the
        contamination filter (> 150 chars).  See #1932."""
        conn = _mock_conn()

        long_name = "C" * 9000
        result = upsert_party(conn, raw_name=long_name, party_type="plaintiff")

        # The contaminated name is rejected — returns empty string
        assert result == ""


# ---------------------------------------------------------------------------
# Contaminated party name filtering (DB safety net) — #1932
# ---------------------------------------------------------------------------


class TestUpsertPartyContaminationFilter:
    """Verify upsert_party rejects contaminated party names."""

    def test_court_header_skipped(self) -> None:
        conn = _mock_conn()
        result = upsert_party(
            conn,
            raw_name="Department 50 Law And Motion Rulings Case Number: 20Stcv41848",
        )
        assert result == ""

    def test_ruling_text_skipped(self) -> None:
        conn = _mock_conn()
        result = upsert_party(conn, raw_name="Before The Court Are The Following")
        assert result == ""

    def test_motion_description_skipped(self) -> None:
        conn = _mock_conn()
        result = upsert_party(conn, raw_name="Motion For Attorney")
        assert result == ""

    def test_valid_name_not_skipped(self) -> None:
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.side_effect = [None, ("party-uuid-1",)]

        result = upsert_party(conn, raw_name="John Doe", party_type="plaintiff")
        assert result == "party-uuid-1"


class TestBatchUpsertPartiesContaminationFilter:
    """Verify batch_upsert_parties skips contaminated party names."""

    def test_contaminated_entries_filtered(self) -> None:
        conn, cur = _mock_conn_for_batch()
        cur.fetchall.side_effect = [[]]
        cur.fetchone.side_effect = [("pid-1",)]
        cur.nextset.side_effect = [False]

        batch_upsert_parties(
            conn,
            "case-1",
            [
                {"name": "John Doe", "role": "plaintiff"},
                {"name": "Law And Motion Rulings", "role": "defendant"},
                {"name": "Before The Court", "role": "defendant"},
            ],
        )

        # Only 1 party should be inserted (the other 2 are contaminated)
        parties_params = cur.executemany.call_args_list[0][0][1]
        assert len(parties_params) == 1

    def test_all_contaminated_skips_entirely(self) -> None:
        conn, cur = _mock_conn_for_batch()

        batch_upsert_parties(
            conn,
            "case-1",
            [
                {"name": "Hearing Date: March 5, 2026", "role": "plaintiff"},
                {"name": "Motion For Summary", "role": "defendant"},
            ],
        )

        # No DB calls should happen when all entries are contaminated
        cur.execute.assert_not_called()


# ---------------------------------------------------------------------------
# _derive_court_code
# ---------------------------------------------------------------------------


class TestDeriveCourtCode:
    """Tests for _derive_court_code helper."""

    def test_simple_county(self) -> None:
        assert _derive_court_code("CA", "Orange") == "ca-orange"

    def test_multi_word_county(self) -> None:
        assert _derive_court_code("CA", "Los Angeles") == "ca-los-angeles"

    def test_preserves_hyphens_in_county(self) -> None:
        assert _derive_court_code("CA", "San Bernardino") == "ca-san-bernardino"

    def test_uppercase_state(self) -> None:
        assert _derive_court_code("TX", "Harris") == "tx-harris"


# ---------------------------------------------------------------------------
# upsert_court
# ---------------------------------------------------------------------------


class TestUpsertCourt:
    """Tests for upsert_court function."""

    def test_returns_court_id(self) -> None:
        conn = _mock_conn()
        result = upsert_court(conn, "CA", "Orange", "Orange County Superior Court")
        assert result == "fake-uuid-1"

    def test_passes_correct_params(self) -> None:
        conn = _mock_conn()
        upsert_court(conn, "CA", "Los Angeles", "LA Superior Court", timezone="America/Chicago")
        args = _get_execute_args(conn)
        assert args == (
            "CA",
            "Los Angeles",
            "LA Superior Court",
            "ca-los-angeles",
            "America/Chicago",
        )

    def test_default_timezone(self) -> None:
        conn = _mock_conn()
        upsert_court(conn, "CA", "Orange", "OC Superior Court")
        args = _get_execute_args(conn)
        assert args[4] == "America/Los_Angeles"

    def test_raises_if_no_row_returned(self) -> None:
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = None
        with pytest.raises(RuntimeError, match="upsert_court returned no row"):
            upsert_court(conn, "CA", "Orange", "OC Court")

    def test_federal_state_passes_correct_params(self) -> None:
        """Federal courts use state='Federal' which exceeds CHAR(2).

        The DB schema was widened to VARCHAR(20) in migration 16 to
        support non-state jurisdictions like 'Federal'. Verify that the
        function passes the full state value through to the DB. (#2219)
        """
        conn = _mock_conn()
        upsert_court(conn, "Federal", "Federal", "CourtListener")
        args = _get_execute_args(conn)
        assert args == (
            "Federal",
            "Federal",
            "CourtListener",
            "federal-federal",
            "America/Los_Angeles",
        )


# ---------------------------------------------------------------------------
# upsert_case — additional tests
# ---------------------------------------------------------------------------


class TestUpsertCase:
    """Tests for upsert_case function beyond NUL stripping."""

    def test_returns_case_id(self) -> None:
        conn = _mock_conn()
        result = upsert_case(conn, case_number="23STCV12345", court_id="court-1")
        assert result == "fake-uuid-1"

    def test_normalizes_case_number(self) -> None:
        conn = _mock_conn()
        upsert_case(conn, case_number=" 23-STCV 12345 ", court_id="court-1")
        args = _get_execute_args(conn)
        # case_number is args[0], normalized is args[1]
        assert args[0] == " 23-STCV 12345 "
        assert args[1] == "23stcv12345"

    def test_case_type_nul_stripped(self) -> None:
        conn = _mock_conn()
        upsert_case(
            conn,
            case_number="23STCV12345",
            court_id="court-1",
            case_type="civil\x00",
        )
        args = _get_execute_args(conn)
        assert "\x00" not in str(args)

    def test_raises_if_no_row_returned(self) -> None:
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = None
        with pytest.raises(RuntimeError, match="upsert_case"):
            upsert_case(conn, case_number="CASE1", court_id="court-1")


# ---------------------------------------------------------------------------
# insert_document
# ---------------------------------------------------------------------------


class TestInsertDocument:
    """Tests for insert_document function."""

    def test_returns_true_for_new_document(self) -> None:
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = (True,)  # is_new = True
        result = insert_document(
            conn,
            document_id="doc-1",
            case_id="case-1",
            court_id="court-1",
            content_format="html",
            content_hash="abc123",
            s3_key="rulings/doc-1.html",
            s3_bucket="judgemind-docs",
            source_url="https://example.com/ruling.html",
            scraper_id="scraper-oc",
            captured_at=datetime(2026, 3, 5, 10, 0, 0),
            hearing_date=date(2026, 3, 10),
        )
        assert result is True

    def test_returns_false_for_existing_document(self) -> None:
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = (False,)  # is_new = False
        result = insert_document(
            conn,
            document_id="doc-1",
            case_id="case-1",
            court_id="court-1",
            content_format="pdf",
            content_hash="abc123",
            s3_key="rulings/doc-1.pdf",
            s3_bucket="judgemind-docs",
            source_url="https://example.com/ruling.pdf",
            scraper_id="scraper-oc",
            captured_at=datetime(2026, 3, 5, 10, 0, 0),
            hearing_date=None,
        )
        assert result is False

    def test_returns_false_when_no_row(self) -> None:
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = None
        result = insert_document(
            conn,
            document_id="doc-1",
            case_id="case-1",
            court_id="court-1",
            content_format="html",
            content_hash="abc123",
            s3_key=None,
            s3_bucket=None,
            source_url="https://example.com",
            scraper_id="scraper-1",
            captured_at=datetime(2026, 3, 5),
            hearing_date=None,
        )
        assert result is False

    def test_format_mapping_html(self) -> None:
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = (True,)
        insert_document(
            conn,
            document_id="doc-1",
            case_id="case-1",
            court_id="court-1",
            content_format="HTML",
            content_hash="abc",
            s3_key=None,
            s3_bucket=None,
            source_url="https://example.com",
            scraper_id="scraper-1",
            captured_at=datetime(2026, 3, 5),
            hearing_date=None,
        )
        args = _get_execute_args(conn)
        # pg_format is args[3]
        assert args[3] == "html"

    def test_format_mapping_pdf(self) -> None:
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = (True,)
        insert_document(
            conn,
            document_id="doc-1",
            case_id="case-1",
            court_id="court-1",
            content_format="pdf",
            content_hash="abc",
            s3_key=None,
            s3_bucket=None,
            source_url="https://example.com",
            scraper_id="scraper-1",
            captured_at=datetime(2026, 3, 5),
            hearing_date=None,
        )
        args = _get_execute_args(conn)
        assert args[3] == "pdf"

    def test_format_mapping_text(self) -> None:
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = (True,)
        insert_document(
            conn,
            document_id="doc-1",
            case_id="case-1",
            court_id="court-1",
            content_format="text",
            content_hash="abc",
            s3_key=None,
            s3_bucket=None,
            source_url="https://example.com",
            scraper_id="scraper-1",
            captured_at=datetime(2026, 3, 5),
            hearing_date=None,
        )
        args = _get_execute_args(conn)
        assert args[3] == "txt"

    def test_sql_includes_last_seen_at_on_insert_and_upsert(self) -> None:
        """Verify the SQL sets last_seen_at on both INSERT and ON CONFLICT.

        last_seen_at must be set to NOW() on initial insert and updated
        to NOW() on upsert so it always reflects the most recent time
        the scraper encountered this document — even for dedup'd re-scrapes
        where the content hasn't changed.  See #986.
        """
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = (True,)
        insert_document(
            conn,
            document_id="doc-1",
            case_id="case-1",
            court_id="court-1",
            content_format="html",
            content_hash="abc123",
            s3_key=None,
            s3_bucket=None,
            source_url="https://example.com",
            scraper_id="scraper-1",
            captured_at=datetime(2026, 3, 5),
            hearing_date=None,
        )
        sql = cur.execute.call_args[0][0]
        # INSERT clause should include last_seen_at column
        assert "last_seen_at" in sql
        # ON CONFLICT clause should update last_seen_at = NOW()
        assert "last_seen_at = NOW()" in sql

    def test_format_mapping_unknown_defaults_html(self) -> None:
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = (True,)
        insert_document(
            conn,
            document_id="doc-1",
            case_id="case-1",
            court_id="court-1",
            content_format="rtf",
            content_hash="abc",
            s3_key=None,
            s3_bucket=None,
            source_url="https://example.com",
            scraper_id="scraper-1",
            captured_at=datetime(2026, 3, 5),
            hearing_date=None,
        )
        args = _get_execute_args(conn)
        assert args[3] == "html"


# ---------------------------------------------------------------------------
# normalize_judge_name
# ---------------------------------------------------------------------------


class TestNormalizeJudgeName:
    """Tests for normalize_judge_name function."""

    def test_last_first_format(self) -> None:
        assert normalize_judge_name("SMITH, JOHN A.") == "John A. Smith"

    def test_first_last_format(self) -> None:
        assert normalize_judge_name("JOHN A. SMITH") == "John A. Smith"

    def test_strips_whitespace(self) -> None:
        assert normalize_judge_name("  Smith,  John A. ") == "John A. Smith"

    def test_strips_hon_prefix(self) -> None:
        assert normalize_judge_name("Hon. Joseph B. Widman") == "Joseph B. Widman"

    def test_strips_judge_prefix(self) -> None:
        assert normalize_judge_name("Judge Bobby P. Luna") == "Bobby P. Luna"

    def test_strips_the_honorable(self) -> None:
        assert normalize_judge_name("The Honorable Jane Doe") == "Jane Doe"

    def test_strips_arbitrator(self) -> None:
        assert normalize_judge_name("Arbitrator Howard B. Miller") == "Howard B. Miller"

    def test_misplaced_jr_suffix(self) -> None:
        assert normalize_judge_name("Jr. Edward B. Moreton") == "Edward B. Moreton Jr."

    def test_returns_none_for_empty(self) -> None:
        assert normalize_judge_name("") is None

    def test_returns_none_for_whitespace(self) -> None:
        assert normalize_judge_name("   ") is None

    def test_returns_none_for_too_long(self) -> None:
        long_name = "A" * 81
        assert normalize_judge_name(long_name) is None

    def test_returns_none_for_garbage_moving_party(self) -> None:
        assert normalize_judge_name("Moving Party filed a motion") is None

    def test_returns_none_for_garbage_ordered(self) -> None:
        assert normalize_judge_name("Is Ordered to appear") is None

    def test_returns_none_for_garbage_plaintiff(self) -> None:
        assert normalize_judge_name("Plaintiff John Doe") is None

    def test_returns_none_for_garbage_year_prefix(self) -> None:
        assert normalize_judge_name("2024 ruling text here") is None

    def test_returns_none_for_garbage_underscores(self) -> None:
        assert normalize_judge_name("____fill_in____") is None

    def test_strips_unicode_replacement_chars(self) -> None:
        result = normalize_judge_name("\ufffdJohn\ufffd Smith")
        assert result == "John Smith"

    def test_collapses_internal_whitespace(self) -> None:
        assert normalize_judge_name("John   A.   Smith") == "John A. Smith"

    def test_generational_suffix_iii(self) -> None:
        result = normalize_judge_name("JOHN SMITH III")
        assert result == "John Smith III"

    def test_generational_suffix_sr(self) -> None:
        result = normalize_judge_name("JOHN SMITH SR.")
        assert result == "John Smith Sr."

    def test_judge_colon_prefix(self) -> None:
        result = normalize_judge_name("Judge: Bobby P. Luna")
        assert result == "Bobby P. Luna"

    def test_returns_none_for_ordered_to(self) -> None:
        assert normalize_judge_name("Ordered to appear next week") is None

    def test_returns_none_for_defendant(self) -> None:
        assert normalize_judge_name("Defendant Smith") is None

    def test_strips_inverted_question_mark_artifacts(self) -> None:
        raw = "\u00bf \u00bf\u00bf \u00bf \u00bf \u00bf Brock T. Hammond\u00bf\u00bf \u00bf"
        assert normalize_judge_name(raw) == "Brock T. Hammond"

    def test_returns_none_for_only_inverted_question_marks(self) -> None:
        assert normalize_judge_name("\u00bf") is None

    def test_strips_zero_width_spaces(self) -> None:
        result = normalize_judge_name("John \u200bA. Smith")
        assert result == "John A. Smith"


# ---------------------------------------------------------------------------
# _looks_like_valid_judge_name
# ---------------------------------------------------------------------------


class TestLooksLikeValidJudgeName:
    """Tests for _looks_like_valid_judge_name helper."""

    def test_valid_two_word_name(self) -> None:
        assert _looks_like_valid_judge_name("John Smith") is True

    def test_valid_three_word_name(self) -> None:
        assert _looks_like_valid_judge_name("John A. Smith") is True

    def test_single_word_rejected(self) -> None:
        assert _looks_like_valid_judge_name("Smith") is False

    def test_empty_string_rejected(self) -> None:
        assert _looks_like_valid_judge_name("") is False

    def test_whitespace_only_rejected(self) -> None:
        assert _looks_like_valid_judge_name("   ") is False


# ---------------------------------------------------------------------------
# resolve_judge
# ---------------------------------------------------------------------------


class TestResolveJudge:
    """Tests for resolve_judge function."""

    def test_returns_existing_alias(self) -> None:
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        # Step 1 SELECT now returns (judge_id, canonical_name) (#3503)
        cur.fetchone.return_value = ("existing-judge-id", "John Smith")
        result = resolve_judge(conn, "Hon. John Smith", "court-1")
        assert result == "existing-judge-id"

    def test_creates_new_judge_when_no_alias(self) -> None:
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        # fetchone: alias lookup -> None, canonical lookup -> None,
        # roster court_code -> None (no court), INSERT -> new id
        cur.fetchone.side_effect = [None, None, None, ("new-judge-id",)]
        cur.fetchall.return_value = []  # no near-duplicates
        result = resolve_judge(conn, "Hon. John Smith", "court-1")
        assert result == "new-judge-id"
        # Should have 6 execute calls:
        # SELECT alias, SELECT canonical, SELECT near-dup,
        # SELECT court_code (roster), INSERT judge, INSERT alias
        assert cur.execute.call_count == 6

    def test_returns_none_for_garbage_name(self) -> None:
        conn = _mock_conn()
        result = resolve_judge(conn, "Moving Party filed a motion", "court-1")
        assert result is None

    def test_returns_none_for_single_word_name(self) -> None:
        conn = _mock_conn()
        result = resolve_judge(conn, "Smith", "court-1")
        assert result is None

    def test_returns_none_for_empty_name(self) -> None:
        conn = _mock_conn()
        result = resolve_judge(conn, "", "court-1")
        assert result is None

    def test_strips_nul_from_raw_name(self) -> None:
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        # Step 1 SELECT now returns (judge_id, canonical_name) (#3503)
        cur.fetchone.return_value = ("existing-judge-id", "John Smith")
        result = resolve_judge(conn, "John\x00 Smith", "court-1")
        assert result == "existing-judge-id"
        # Verify NUL was stripped from the name passed to SQL
        select_args = cur.execute.call_args_list[0][0][1]
        assert "\x00" not in str(select_args)

    def test_raises_on_insert_returning_none(self) -> None:
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        # fetchone: alias lookup -> None, canonical lookup -> None,
        # roster court_code -> None (no court), INSERT -> None
        cur.fetchone.side_effect = [None, None, None, None]
        cur.fetchall.return_value = []  # no near-duplicates
        with pytest.raises(RuntimeError, match="resolve_judge"):
            resolve_judge(conn, "John Smith", "court-1")

    def test_lookup_uses_case_insensitive_match(self) -> None:
        """Verify alias lookup uses LOWER() for case-insensitive matching (#1453)."""
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        # Step 1 SELECT now returns (judge_id, canonical_name) (#3503)
        cur.fetchone.return_value = ("existing-judge-id", "John Smith")
        resolve_judge(conn, "HON. JOHN SMITH", "court-1")
        sql = cur.execute.call_args_list[0][0][0]
        assert "LOWER" in sql


# ---------------------------------------------------------------------------
# upsert_case_judge
# ---------------------------------------------------------------------------


class TestUpsertCaseJudge:
    """Tests for upsert_case_judge function."""

    def test_inserts_with_hearing_date(self) -> None:
        conn = _mock_conn()
        upsert_case_judge(conn, "case-1", "judge-1", date(2026, 3, 10))
        args = _get_execute_args(conn)
        assert args == ("case-1", "judge-1", date(2026, 3, 10))

    def test_inserts_with_none_hearing_date(self) -> None:
        conn = _mock_conn()
        upsert_case_judge(conn, "case-1", "judge-1", None)
        args = _get_execute_args(conn)
        assert args == ("case-1", "judge-1", None)

    def test_execute_called_once(self) -> None:
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        upsert_case_judge(conn, "case-1", "judge-1", None)
        assert cur.execute.call_count == 1


# ---------------------------------------------------------------------------
# upsert_party — existing alias path
# ---------------------------------------------------------------------------


class TestUpsertPartyExistingAlias:
    """Verify upsert_party returns existing party_id when alias exists."""

    def test_returns_existing_party_id(self) -> None:
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = ("existing-party-id",)
        result = upsert_party(conn, raw_name="John Doe", party_type="plaintiff")
        assert result == "existing-party-id"
        # Should only have the SELECT call, no INSERT
        assert cur.execute.call_count == 1

    def test_raises_if_insert_returns_none(self) -> None:
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        # First fetchone: no alias; second: INSERT returns None
        cur.fetchone.side_effect = [None, None]
        with pytest.raises(RuntimeError, match="upsert_party"):
            upsert_party(conn, raw_name="John Doe", party_type="plaintiff")

    def test_lookup_uses_case_insensitive_match(self) -> None:
        """Verify alias lookup uses LOWER() for case-insensitive matching (#1426)."""
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = ("existing-party-id",)
        upsert_party(conn, raw_name="JOHN DOE", party_type="plaintiff")
        sql = cur.execute.call_args_list[0][0][0]
        assert "LOWER" in sql


# ---------------------------------------------------------------------------
# upsert_case_party
# ---------------------------------------------------------------------------


class TestUpsertCaseParty:
    """Tests for upsert_case_party function."""

    def test_passes_correct_params(self) -> None:
        conn = _mock_conn()
        upsert_case_party(conn, "case-1", "party-1", "plaintiff")
        args = _get_execute_args(conn)
        assert args == ("case-1", "party-1", "plaintiff")

    def test_uses_on_conflict_do_nothing(self) -> None:
        """Verify the SQL uses ON CONFLICT DO NOTHING on the business key."""
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        upsert_case_party(conn, "case-1", "party-1", "plaintiff")
        sql = cur.execute.call_args[0][0]
        assert "ON CONFLICT" in sql
        assert "DO NOTHING" in sql

    def test_execute_called_once(self) -> None:
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        upsert_case_party(conn, "case-1", "party-1", "defendant")
        assert cur.execute.call_count == 1


# ---------------------------------------------------------------------------
# normalize_party_name
# ---------------------------------------------------------------------------


class TestNormalizePartyName:
    """Tests for normalize_party_name function."""

    def test_title_cases(self) -> None:
        assert normalize_party_name("JOHN DOE") == "John Doe"

    def test_strips_whitespace(self) -> None:
        assert normalize_party_name("  John Doe  ") == "John Doe"

    def test_collapses_internal_whitespace(self) -> None:
        assert normalize_party_name("John   Doe") == "John Doe"


# ---------------------------------------------------------------------------
# normalize_judge_name — encoding artifact stripping
# ---------------------------------------------------------------------------


class TestNormalizeJudgeNameEncodingArtifacts:
    """Tests for encoding artifact removal in normalize_judge_name."""

    def test_strips_inverted_question_mark(self) -> None:
        """U+00BF (¿) from Windows-1252 misinterpretation is removed."""
        result = normalize_judge_name("John\u00bf Smith")
        assert result == "John Smith"

    def test_replaces_nbsp_with_space(self) -> None:
        """U+00A0 (non-breaking space) is replaced with a regular space."""
        result = normalize_judge_name("John\u00a0Smith")
        assert result == "John Smith"

    def test_strips_soft_hyphen(self) -> None:
        """U+00AD (soft hyphen) is removed."""
        result = normalize_judge_name("John\u00ad Smith")
        assert result == "John Smith"

    def test_preserves_accented_characters(self) -> None:
        """Accented Latin characters (À-ÿ) are preserved."""
        result = normalize_judge_name("José García")
        assert result == "José García"

    def test_strips_multiple_artifacts(self) -> None:
        """Multiple encoding artifacts are stripped in one pass."""
        result = normalize_judge_name("\u00bfJohn\u00a0\u00adSmith\u00bf")
        assert result == "John Smith"

    def test_strips_zero_width_space(self) -> None:
        """U+200B (zero-width space) is removed."""
        result = normalize_judge_name("John\u200b Smith")
        assert result == "John Smith"

    def test_preserves_period_hyphen_apostrophe(self) -> None:
        """Periods, hyphens, and apostrophes are preserved."""
        result = normalize_judge_name("John O'Brien-Smith Jr.")
        assert result == "John O'Brien-Smith Jr."


# ---------------------------------------------------------------------------
# _looks_like_valid_judge_name — truncated name detection
# ---------------------------------------------------------------------------


class TestLooksLikeValidJudgeNameTruncation:
    """Tests for truncated name detection in _looks_like_valid_judge_name."""

    def test_rejects_truncated_mc(self) -> None:
        """'Melissa R. Mc' is clearly truncated."""
        assert _looks_like_valid_judge_name("Melissa R. Mc") is False

    def test_rejects_truncated_kin(self) -> None:
        """'Curtis A. Kin' is clearly truncated (3 chars, no vowel)."""
        assert _looks_like_valid_judge_name("Curtis A. Kin") is True  # has vowel 'i'

    def test_rejects_two_char_surname(self) -> None:
        """'John Ab' is likely truncated."""
        assert _looks_like_valid_judge_name("John Ab") is False

    def test_rejects_bare_initial_ending(self) -> None:
        """'John Smith A' ending in a bare initial is rejected."""
        assert _looks_like_valid_judge_name("John Smith A") is False

    def test_accepts_valid_suffix_jr(self) -> None:
        """'Edward B. Moreton Jr.' is valid (Jr. is a known suffix)."""
        assert _looks_like_valid_judge_name("Edward B. Moreton Jr.") is True

    def test_accepts_valid_suffix_iii(self) -> None:
        """'John Smith III' is valid (III is a known suffix)."""
        assert _looks_like_valid_judge_name("John Smith III") is True

    def test_accepts_normal_name(self) -> None:
        """Normal names pass validation."""
        assert _looks_like_valid_judge_name("Carmen R. Luege") is True

    def test_accepts_short_real_surname(self) -> None:
        """'John Lee' has a 3-char surname with a vowel — valid."""
        assert _looks_like_valid_judge_name("John Lee") is True

    def test_rejects_consonant_only_short_surname(self) -> None:
        """'James Bnk' — no vowels, 3 chars — likely truncated."""
        assert _looks_like_valid_judge_name("James Bnk") is False

    def test_accepts_longer_consonant_surname(self) -> None:
        """'John Hrdlk' — 5+ chars, even without vowels, passes."""
        assert _looks_like_valid_judge_name("John Hrdlk") is True

    def test_rejects_single_char_surname(self) -> None:
        """'John K' — single character, not a suffix — rejected."""
        assert _looks_like_valid_judge_name("John K") is False

    @pytest.mark.parametrize("surname", ["Vu", "Lo", "Li", "Wu", "Xu", "Hu", "Lu", "Fu", "Ng"])
    @pytest.mark.parametrize("trailing", ["", "."])
    def test_accepts_asian_surname_all_forms(self, surname: str, trailing: str) -> None:
        """All 9 Asian surnames are valid in both plain and trailing-period forms."""
        assert _looks_like_valid_judge_name(f"Nathan {surname}{trailing}") is True

    def test_accepts_trailing_period_after_asian_surname(self) -> None:
        # AC1: trailing period from court-supplied directory should not reject
        assert _looks_like_valid_judge_name("Hon. Nathan Vu.") is True


# ---------------------------------------------------------------------------
# _strip_middle_initials
# ---------------------------------------------------------------------------


class TestStripMiddleInitials:
    """Tests for _strip_middle_initials helper."""

    def test_removes_single_initial(self) -> None:
        assert _strip_middle_initials("Carmen R. Luege") == "Carmen Luege"

    def test_removes_multiple_initials(self) -> None:
        assert _strip_middle_initials("John A. B. Smith") == "John Smith"

    def test_preserves_two_word_name(self) -> None:
        assert _strip_middle_initials("John Smith") == "John Smith"

    def test_preserves_middle_name(self) -> None:
        """Full middle names (not initials) are kept."""
        assert _strip_middle_initials("John Robert Smith") == "John Robert Smith"

    def test_preserves_single_word(self) -> None:
        assert _strip_middle_initials("Smith") == "Smith"

    def test_strips_initial_without_period(self) -> None:
        """Bare initials without periods are also stripped."""
        assert _strip_middle_initials("Carmen R Luege") == "Carmen Luege"

    def test_mixed_initials_and_names(self) -> None:
        """Only single-letter-with-period words are stripped."""
        assert _strip_middle_initials("John A. Robert B. Smith") == "John Robert Smith"


# ---------------------------------------------------------------------------
# resolve_judge — canonical name lookup
# ---------------------------------------------------------------------------


class TestResolveJudgeCanonicalLookup:
    """Tests for the canonical_name lookup step in resolve_judge."""

    def test_finds_existing_judge_by_canonical_name(self) -> None:
        """When alias lookup fails but canonical_name exists, reuse the judge."""
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        # fetchone sequence:
        # 1. alias lookup -> None (no alias)
        # 2. canonical lookup -> existing judge id
        cur.fetchone.side_effect = [None, ("existing-judge-id",)]
        result = resolve_judge(conn, "Hon. John Smith", "court-1")
        assert result == "existing-judge-id"
        # Should have: SELECT alias, SELECT canonical, INSERT alias
        assert cur.execute.call_count == 3

    def test_creates_alias_for_canonical_match(self) -> None:
        """An alias is created when matching by canonical name."""
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.side_effect = [None, ("existing-judge-id",)]
        resolve_judge(conn, "JOHN SMITH", "court-1")
        # The third execute call should be the alias INSERT
        alias_sql = cur.execute.call_args_list[2][0][0]
        assert "judge_aliases" in alias_sql
        assert "ON CONFLICT DO NOTHING" in alias_sql


# ---------------------------------------------------------------------------
# resolve_judge — near-duplicate detection
# ---------------------------------------------------------------------------


class TestResolveJudgeNearDuplicate:
    """Tests for near-duplicate detection in resolve_judge."""

    def test_matches_name_without_middle_initial(self) -> None:
        """'Carmen Luege' should match existing 'Carmen R. Luege'."""
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        # fetchone sequence:
        # 1. alias lookup -> None
        # 2. canonical lookup -> None
        # fetchall: near-dup search -> one existing judge
        cur.fetchone.side_effect = [None, None]
        cur.fetchall.return_value = [
            ("existing-judge-id", "Carmen R. Luege"),
        ]
        result = resolve_judge(conn, "Carmen Luege", "court-1")
        assert result == "existing-judge-id"

    def test_updates_canonical_when_new_is_more_complete(self) -> None:
        """'Carmen R. Luege' should update canonical from 'Carmen Luege'."""
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.side_effect = [None, None]
        cur.fetchall.return_value = [
            ("existing-judge-id", "Carmen Luege"),
        ]
        resolve_judge(conn, "Carmen R. Luege", "court-1")
        # Should include an UPDATE to canonical_name
        update_calls = [c for c in cur.execute.call_args_list if "UPDATE judges" in c[0][0]]
        assert len(update_calls) == 1
        assert update_calls[0][0][1][0] == "Carmen R. Luege"

    def test_no_update_when_existing_is_more_complete(self) -> None:
        """When existing name is more complete, don't update canonical."""
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.side_effect = [None, None]
        cur.fetchall.return_value = [
            ("existing-judge-id", "Carmen R. Luege"),
        ]
        resolve_judge(conn, "Carmen Luege", "court-1")
        # Should NOT include an UPDATE to canonical_name
        update_calls = [c for c in cur.execute.call_args_list if "UPDATE judges" in c[0][0]]
        assert len(update_calls) == 0

    def test_near_dup_alias_uses_lower_confidence(self) -> None:
        """Near-duplicate aliases should use 0.9 confidence, not 1.0."""
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.side_effect = [None, None]
        cur.fetchall.return_value = [
            ("existing-judge-id", "Carmen R. Luege"),
        ]
        resolve_judge(conn, "Carmen Luege", "court-1")
        # Find the alias INSERT call
        alias_calls = [
            c
            for c in cur.execute.call_args_list
            if "judge_aliases" in c[0][0] and "INSERT" in c[0][0]
        ]
        assert len(alias_calls) == 1
        # Confidence should be 0.9 for near-duplicate
        assert "0.9" in alias_calls[0][0][0]

    def test_creates_new_judge_when_no_match(self) -> None:
        """When no alias, canonical, near-dup, or roster match exists, create new judge."""
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        # fetchone sequence:
        # 1. alias lookup -> None
        # 2. canonical lookup -> None
        # 3. roster court_code -> None (no court)
        # 4. INSERT judges -> new id
        cur.fetchone.side_effect = [None, None, None, ("new-judge-id",)]
        cur.fetchall.return_value = []  # no existing judges at court
        result = resolve_judge(conn, "John Smith", "court-1")
        assert result == "new-judge-id"

    def test_new_judge_uses_on_conflict(self) -> None:
        """The INSERT INTO judges uses ON CONFLICT for race condition safety."""
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        # fetchone: alias -> None, canonical -> None,
        # roster court_code -> None, INSERT -> new id
        cur.fetchone.side_effect = [None, None, None, ("new-judge-id",)]
        cur.fetchall.return_value = []
        resolve_judge(conn, "John Smith", "court-1")
        # Find the INSERT INTO judges call
        insert_calls = [c for c in cur.execute.call_args_list if "INSERT INTO judges" in c[0][0]]
        assert len(insert_calls) == 1
        assert "ON CONFLICT" in insert_calls[0][0][0]


# ---------------------------------------------------------------------------
# normalize_ruling_text_hash
# ---------------------------------------------------------------------------


class TestNormalizeRulingTextHash:
    """Unit tests for normalize_ruling_text_hash."""

    def test_returns_none_for_none(self) -> None:
        assert normalize_ruling_text_hash(None) is None

    def test_returns_none_for_empty_string(self) -> None:
        assert normalize_ruling_text_hash("") is None

    def test_returns_none_for_whitespace_only(self) -> None:
        assert normalize_ruling_text_hash("   \n\t  ") is None

    def test_same_text_different_case_produces_same_hash(self) -> None:
        """Title Case and ALL CAPS produce the same hash."""
        h1 = normalize_ruling_text_hash("Motion to Compel Discovery GRANTED")
        h2 = normalize_ruling_text_hash("MOTION TO COMPEL DISCOVERY GRANTED")
        h3 = normalize_ruling_text_hash("motion to compel discovery granted")
        assert h1 == h2 == h3

    def test_different_whitespace_produces_same_hash(self) -> None:
        """Different whitespace patterns produce the same hash."""
        h1 = normalize_ruling_text_hash("Motion  to   Compel")
        h2 = normalize_ruling_text_hash("Motion to Compel")
        h3 = normalize_ruling_text_hash("Motion\n\tto\nCompel")
        h4 = normalize_ruling_text_hash("  Motion to Compel  ")
        assert h1 == h2 == h3 == h4

    def test_different_text_produces_different_hash(self) -> None:
        h1 = normalize_ruling_text_hash("Motion GRANTED")
        h2 = normalize_ruling_text_hash("Motion DENIED")
        assert h1 != h2

    def test_returns_hex_string(self) -> None:
        """Returns a 64-char hex string (SHA-256)."""
        result = normalize_ruling_text_hash("Some ruling text.")
        assert result is not None
        assert len(result) == 64
        assert all(c in "0123456789abcdef" for c in result)


# ---------------------------------------------------------------------------
# insert_ruling — content-hash dedup
# ---------------------------------------------------------------------------


class TestInsertRulingContentDedup:
    """Verify insert_ruling content-hash dedup behavior."""

    def test_insert_includes_ruling_text_hash(self) -> None:
        """The INSERT includes ruling_text_hash as the last parameter."""
        conn = _mock_conn()
        insert_ruling(
            conn,
            document_id="doc-1",
            case_id="case-1",
            court_id="court-1",
            hearing_date=date(2026, 3, 5),
            ruling_text="Motion GRANTED",
            department="Dept. 1",
        )

        cur = conn.cursor.return_value.__enter__.return_value
        # Find the INSERT call and check that ruling_text_hash is included
        insert_calls = [c for c in cur.execute.call_args_list if "INSERT INTO rulings" in c[0][0]]
        assert len(insert_calls) == 1
        sql = insert_calls[0][0][0]
        args = insert_calls[0][0][1]
        assert "ruling_text_hash" in sql
        # text_hash is the last argument
        expected_hash = normalize_ruling_text_hash("Motion GRANTED")
        assert args[-1] == expected_hash

    def test_insert_with_none_ruling_text_has_null_hash(self) -> None:
        """When ruling_text is None, ruling_text_hash should be None."""
        conn = _mock_conn()
        insert_ruling(
            conn,
            document_id="doc-1",
            case_id="case-1",
            court_id="court-1",
            hearing_date=date(2026, 3, 5),
            ruling_text=None,
            department="Dept. 1",
        )

        cur = conn.cursor.return_value.__enter__.return_value
        insert_calls = [c for c in cur.execute.call_args_list if "INSERT INTO rulings" in c[0][0]]
        assert len(insert_calls) == 1
        args = insert_calls[0][0][1]
        # text_hash (last arg) should be None
        assert args[-1] is None

    def test_unique_violation_triggers_supersede(self) -> None:
        """When UniqueViolation fires on ``uq_rulings_case_text_hash``,
        supersede the losing document instead of falling back to a
        content-hash UPDATE (#2458).

        In tests without a real PG connection, diag.constraint_name is None.
        The code treats None as the expected constraint (see db.py comment).
        """
        import psycopg.errors

        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        exc = psycopg.errors.UniqueViolation("duplicate key value violates unique constraint")

        def side_effect_execute(sql: str, params: tuple | None = None) -> None:
            if "INSERT INTO rulings" in sql:
                raise exc

        cur.execute = MagicMock(side_effect=side_effect_execute)

        insert_ruling(
            conn,
            document_id="new-doc-1",
            case_id="case-1",
            court_id="court-1",
            hearing_date=date(2026, 3, 5),
            ruling_text="Motion GRANTED",
            department="Dept. 1",
        )

        execute_calls = cur.execute.call_args_list
        sql_stmts = [call[0][0] for call in execute_calls]
        # Savepoint-rollback path still runs.
        assert any("SAVEPOINT" in s for s in sql_stmts)
        assert any("INSERT INTO rulings" in s for s in sql_stmts)
        assert any("ROLLBACK TO SAVEPOINT" in s for s in sql_stmts)
        # New behavior: supersede the losing document.  No content-hash UPDATE.
        assert any("DELETE FROM rulings WHERE document_id" in s for s in sql_stmts)
        assert any("UPDATE documents SET status = 'superseded'" in s for s in sql_stmts)
        # The old buggy fallback UPDATE targeting (case_id, ruling_text_hash)
        # must NOT run — that was the #2458 bug.
        assert not any("UPDATE rulings SET" in s and "ruling_text_hash" in s for s in sql_stmts), (
            "Old fallback UPDATE-by-content-hash must not run — it silently "
            "mutated the winner's row with the loser's fields (#2458)."
        )

    def test_unique_violation_supersede_uses_losing_document_id(self) -> None:
        """The DELETE and UPDATE target the current (losing) document_id."""
        import psycopg.errors

        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        exc = psycopg.errors.UniqueViolation("duplicate key value violates unique constraint")

        def side_effect_execute(sql: str, params: tuple | None = None) -> None:
            if "INSERT INTO rulings" in sql:
                raise exc

        cur.execute = MagicMock(side_effect=side_effect_execute)

        insert_ruling(
            conn,
            document_id="losing-doc",
            case_id="case-1",
            court_id="court-1",
            hearing_date=date(2026, 3, 5),
            ruling_text="Motion GRANTED",
            department="Dept. 1",
        )

        execute_calls = cur.execute.call_args_list
        delete_calls = [c for c in execute_calls if "DELETE FROM rulings" in c[0][0]]
        assert len(delete_calls) == 1
        assert delete_calls[0][0][1] == ("losing-doc",), (
            "DELETE must target the losing document_id, not the winner's."
        )

        doc_update_calls = [
            c for c in execute_calls if "UPDATE documents" in c[0][0] and "superseded" in c[0][0]
        ]
        assert len(doc_update_calls) == 1
        # The loser's document_id is always the LAST positional param
        # (after #2569, the UPDATE also carries the winner_id in earlier
        # positions when a winner is resolved).
        update_params = doc_update_calls[0][0][1]
        assert update_params[-1] == "losing-doc", (
            f"UPDATE documents must target the losing document_id (got params={update_params!r})."
        )

    def test_unique_violation_supersede_in_force_update_mode(self) -> None:
        """``force_update=True`` (reingest path) still supersedes the loser.

        Supersede semantics do not depend on ``force_update`` — if the
        (case_id, ruling_text_hash) constraint fires, the incoming document's
        text is by definition identical to the winner's, so the loser must
        drop out regardless of whether we are in live-ingestion or reingest.
        """
        import psycopg.errors

        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        exc = psycopg.errors.UniqueViolation("duplicate key value violates unique constraint")

        def side_effect_execute(sql: str, params: tuple | None = None) -> None:
            if "INSERT INTO rulings" in sql:
                raise exc

        cur.execute = MagicMock(side_effect=side_effect_execute)

        insert_ruling(
            conn,
            document_id="losing-doc",
            case_id="case-1",
            court_id="court-1",
            hearing_date=date(2026, 3, 5),
            ruling_text="Motion GRANTED",
            department="Dept. 1",
            judge_id="judge-1",
            outcome="granted",
            force_update=True,
        )

        sql_stmts = [call[0][0] for call in cur.execute.call_args_list]
        assert any("DELETE FROM rulings WHERE document_id" in s for s in sql_stmts)
        assert any("UPDATE documents SET status = 'superseded'" in s for s in sql_stmts)
        # Under no mode should the old fallback UPDATE run.
        assert not any("UPDATE rulings SET" in s and "ruling_text_hash" in s for s in sql_stmts)

    def test_supersede_populates_previous_version_id(self) -> None:
        """Loser doc's ``previous_version_id`` is set to the winner's id (#2569).

        Without this link, loser docs look like "zero derived rulings" to
        naive spotcheck queries that filter on ``documents.s3_key``.  The
        link lets downstream tooling trace the loser back to the
        canonical rulings on the winner.
        """
        import psycopg.errors

        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        exc = psycopg.errors.UniqueViolation("duplicate key value violates unique constraint")

        def side_effect_execute(sql: str, params: tuple | None = None) -> None:
            if "INSERT INTO rulings" in sql:
                raise exc

        cur.execute = MagicMock(side_effect=side_effect_execute)
        # First fetchone() — winner lookup — returns the winner id.
        # Second fetchone() — county/s3_key — returns a 1-tuple (mock
        # default); the except clause in db.py swallows the resulting
        # IndexError, which is fine for this test.
        cur.fetchone = MagicMock(return_value=("winner-doc-id",))

        insert_ruling(
            conn,
            document_id="losing-doc",
            case_id="case-1",
            court_id="court-1",
            hearing_date=date(2026, 3, 5),
            ruling_text="Motion GRANTED",
            department="Dept. 1",
        )

        execute_calls = cur.execute.call_args_list
        # The UPDATE documents statement should set previous_version_id.
        doc_update_calls = [
            c for c in execute_calls if "UPDATE documents" in c[0][0] and "superseded" in c[0][0]
        ]
        assert len(doc_update_calls) == 1
        update_sql = doc_update_calls[0][0][0]
        assert "previous_version_id = %s::uuid" in update_sql, (
            "Supersede UPDATE must set previous_version_id to the winner's id."
        )
        assert "change_type = 'duplicate_content'" in update_sql, (
            "Supersede UPDATE must set change_type='duplicate_content' "
            "for downstream classification."
        )
        # Params are (winner_id, loser_id).
        update_params = doc_update_calls[0][0][1]
        assert update_params == ("winner-doc-id", "losing-doc"), (
            f"UPDATE params must be (winner_id, loser_id); got {update_params!r}"
        )

    def test_supersede_queries_winner_document_id(self) -> None:
        """The supersede path first queries the winner's document_id.

        It joins on (case_id, ruling_text_hash) — the same pair that
        triggered the UniqueViolation.
        """
        import psycopg.errors

        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        exc = psycopg.errors.UniqueViolation("duplicate key value violates unique constraint")

        def side_effect_execute(sql: str, params: tuple | None = None) -> None:
            if "INSERT INTO rulings" in sql:
                raise exc

        cur.execute = MagicMock(side_effect=side_effect_execute)
        cur.fetchone = MagicMock(return_value=("winner-doc-id",))

        insert_ruling(
            conn,
            document_id="losing-doc",
            case_id="case-1",
            court_id="court-1",
            hearing_date=date(2026, 3, 5),
            ruling_text="Motion GRANTED",
            department="Dept. 1",
        )

        execute_calls = cur.execute.call_args_list
        winner_lookup_calls = [
            c
            for c in execute_calls
            if "SELECT document_id" in c[0][0] and "FROM rulings" in c[0][0]
        ]
        assert len(winner_lookup_calls) >= 1, (
            "Supersede path must query the winner's document_id before the UPDATE."
        )

    def test_supersede_falls_back_when_winner_not_found(self) -> None:
        """If the winner lookup returns no row, still supersede the loser.

        Defensive branch — the UniqueViolation means a winner SHOULD
        exist, but if the mock/fetchone returns None the supersede path
        must still run and mark the loser with change_type so reporting
        is not silent.
        """
        import psycopg.errors

        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        exc = psycopg.errors.UniqueViolation("duplicate key value violates unique constraint")

        def side_effect_execute(sql: str, params: tuple | None = None) -> None:
            if "INSERT INTO rulings" in sql:
                raise exc

        cur.execute = MagicMock(side_effect=side_effect_execute)
        cur.fetchone = MagicMock(return_value=None)

        insert_ruling(
            conn,
            document_id="losing-doc",
            case_id="case-1",
            court_id="court-1",
            hearing_date=date(2026, 3, 5),
            ruling_text="Motion GRANTED",
            department="Dept. 1",
        )

        execute_calls = cur.execute.call_args_list
        doc_update_calls = [
            c for c in execute_calls if "UPDATE documents" in c[0][0] and "superseded" in c[0][0]
        ]
        assert len(doc_update_calls) == 1
        update_sql = doc_update_calls[0][0][0]
        # Fallback UPDATE: status + change_type but NO previous_version_id.
        assert "change_type = 'duplicate_content'" in update_sql
        assert "previous_version_id" not in update_sql, (
            "Fallback UPDATE (no winner found) must omit previous_version_id."
        )

    def test_supersede_logs_warning_with_structured_fields(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The supersede path emits a WARN-level log with structured fields (#2569).

        Promoted from info to warning so operators can alert on this
        class of dedup event.
        """
        import logging

        import psycopg.errors

        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        exc = psycopg.errors.UniqueViolation("duplicate key value violates unique constraint")

        def side_effect_execute(sql: str, params: tuple | None = None) -> None:
            if "INSERT INTO rulings" in sql:
                raise exc

        cur.execute = MagicMock(side_effect=side_effect_execute)
        cur.fetchone = MagicMock(return_value=("winner-doc-id",))

        caplog.set_level(logging.WARNING, logger="ingestion.db")
        insert_ruling(
            conn,
            document_id="losing-doc",
            case_id="case-1",
            court_id="court-1",
            hearing_date=date(2026, 3, 5),
            ruling_text="Motion GRANTED",
            department="Dept. 1",
        )

        warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warning_records, (
            "Supersede path must emit at least one WARN-level log (promoted from info in #2569)."
        )
        rec = warning_records[0]
        assert "content-hash dedup" in rec.getMessage().lower()
        # Structured fields available via ``extra``.
        assert getattr(rec, "loser_document_id", None) == "losing-doc"
        assert getattr(rec, "winner_document_id", None) == "winner-doc-id"
        assert getattr(rec, "event", None) == "content_hash_dedup_supersede"

    def test_supersede_writes_data_quality_metric(self) -> None:
        """The supersede path emits a ``content_hash_dedup_supersede`` metric (#2569).

        This gives the previously-silent dedup path a dashboard-queryable
        observability signal so the 50%+ content-hash dedup rate in
        multi-case-PDF counties is visible.
        """
        import psycopg.errors

        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        exc = psycopg.errors.UniqueViolation("duplicate key value violates unique constraint")

        def side_effect_execute(sql: str, params: tuple | None = None) -> None:
            if "INSERT INTO rulings" in sql:
                raise exc

        cur.execute = MagicMock(side_effect=side_effect_execute)
        # fetchone() returns values for (a) winner lookup, (b) county/s3
        # lookup.  Two-tuple on the second call so county branch is taken.
        cur.fetchone = MagicMock(
            side_effect=[
                ("winner-doc-id",),
                ("Contra Costa", "ca-contra_costa/some.pdf"),
            ]
        )

        insert_ruling(
            conn,
            document_id="losing-doc",
            case_id="case-1",
            court_id="court-1",
            hearing_date=date(2026, 3, 5),
            ruling_text="Motion GRANTED",
            department="Dept. 1",
        )

        execute_calls = cur.execute.call_args_list
        metric_calls = [c for c in execute_calls if "INSERT INTO data_quality_metrics" in c[0][0]]
        assert len(metric_calls) == 1, (
            "Supersede path must write exactly one data_quality_metrics "
            "row when the county is resolvable."
        )
        metric_params = metric_calls[0][0][1]
        # (county, metric_name, metric_value, metadata_json)
        assert metric_params[0] == "Contra Costa"
        assert metric_params[1] == "content_hash_dedup_supersede"
        assert metric_params[2] == 1
        # metadata is a JSON string containing loser/winner/case/s3 fields
        import json as _json

        meta = _json.loads(metric_params[3])
        assert meta["loser_document_id"] == "losing-doc"
        assert meta["winner_document_id"] == "winner-doc-id"
        assert meta["case_id"] == "case-1"
        assert meta["s3_key"] == "ca-contra_costa/some.pdf"

    def test_supersede_skips_metric_when_county_unresolvable(self) -> None:
        """If the context lookup fails, the primary supersede still succeeds.

        The metric write is skipped — we never let a best-effort
        telemetry write break the primary supersede path (#2569).
        """
        import psycopg.errors

        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        exc = psycopg.errors.UniqueViolation("duplicate key value violates unique constraint")

        def side_effect_execute(sql: str, params: tuple | None = None) -> None:
            if "INSERT INTO rulings" in sql:
                raise exc

        cur.execute = MagicMock(side_effect=side_effect_execute)
        # Winner lookup returns a row; county lookup returns None.
        cur.fetchone = MagicMock(side_effect=[("winner-doc-id",), None])

        insert_ruling(
            conn,
            document_id="losing-doc",
            case_id="case-1",
            court_id="court-1",
            hearing_date=date(2026, 3, 5),
            ruling_text="Motion GRANTED",
            department="Dept. 1",
        )

        execute_calls = cur.execute.call_args_list
        sql_stmts = [call[0][0] for call in execute_calls]
        # Supersede still ran.
        assert any("UPDATE documents" in s and "superseded" in s for s in sql_stmts)
        # Metric was skipped.
        assert not any("INSERT INTO data_quality_metrics" in s for s in sql_stmts), (
            "Metric write must be skipped when county is unresolvable."
        )

    def test_unknown_constraint_violation_is_reraised(self) -> None:
        """UniqueViolation from an unrelated constraint should be re-raised.

        We use a subclass to override the read-only diag property so that
        constraint_name returns a non-matching value.
        """
        import psycopg.errors

        class _MockViolation(psycopg.errors.UniqueViolation):
            @property
            def diag(self) -> MagicMock:
                m = MagicMock()
                m.constraint_name = "some_other_constraint"
                return m

        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        exc = _MockViolation("duplicate key value violates unique constraint")

        def side_effect_execute(sql: str, params: tuple | None = None) -> None:
            if "INSERT INTO rulings" in sql:
                raise exc

        cur.execute = MagicMock(side_effect=side_effect_execute)

        with pytest.raises(psycopg.errors.UniqueViolation):
            insert_ruling(
                conn,
                document_id="new-doc-1",
                case_id="case-1",
                court_id="court-1",
                hearing_date=date(2026, 3, 5),
                ruling_text="Motion GRANTED",
                department="Dept. 1",
            )

    def test_on_conflict_document_id_preserves_ruling_text_hash(self) -> None:
        """ON CONFLICT (document_id) DO UPDATE should preserve ruling_text_hash via COALESCE."""
        conn = _mock_conn()
        insert_ruling(
            conn,
            document_id="doc-1",
            case_id="case-1",
            court_id="court-1",
            hearing_date=date(2026, 3, 5),
            ruling_text="Motion GRANTED",
            department="Dept. 1",
        )

        cur = conn.cursor.return_value.__enter__.return_value
        insert_calls = [c for c in cur.execute.call_args_list if "INSERT INTO rulings" in c[0][0]]
        assert len(insert_calls) == 1
        sql = insert_calls[0][0][0]
        assert "ruling_text_hash = COALESCE(" in sql
        assert "EXCLUDED.ruling_text_hash, rulings.ruling_text_hash)" in sql

    def test_savepoint_used_for_insert(self) -> None:
        """INSERT uses a savepoint so UniqueViolation doesn't abort the transaction."""
        conn = _mock_conn()
        insert_ruling(
            conn,
            document_id="doc-1",
            case_id="case-1",
            court_id="court-1",
            hearing_date=date(2026, 3, 5),
            ruling_text="Motion GRANTED",
            department="Dept. 1",
        )

        cur = conn.cursor.return_value.__enter__.return_value
        execute_calls = cur.execute.call_args_list
        sql_stmts = [call[0][0] for call in execute_calls]
        assert "SAVEPOINT ruling_insert" in sql_stmts
        assert "RELEASE SAVEPOINT ruling_insert" in sql_stmts

    def test_supersede_context_select_failure_does_not_abort_supersede(self) -> None:
        """A failing context SELECT must not abort the supersede — SAVEPOINT protects it.

        When ``cur.execute`` raises on the context lookup, the primary supersede
        (DELETE FROM rulings + UPDATE documents) must still run, and
        ``ROLLBACK TO SAVEPOINT supersede_ctx`` must have been issued.
        """
        import psycopg.errors

        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        unique_exc = psycopg.errors.UniqueViolation(
            "duplicate key value violates unique constraint"
        )
        context_exc = psycopg.errors.QueryCanceled("query was canceled")

        def side_effect_execute(sql: str, params: tuple | None = None) -> None:
            if "INSERT INTO rulings" in sql:
                raise unique_exc
            if "SELECT c.county" in sql:
                raise context_exc

        cur.execute = MagicMock(side_effect=side_effect_execute)
        # Winner lookup returns a row; context lookup raises (never reaches fetchone).
        cur.fetchone = MagicMock(return_value=("winner-doc-id",))

        insert_ruling(
            conn,
            document_id="losing-doc",
            case_id="case-1",
            court_id="court-1",
            hearing_date=date(2026, 3, 5),
            ruling_text="Motion GRANTED",
            department="Dept. 1",
        )

        execute_calls = cur.execute.call_args_list
        sql_stmts = [call[0][0] for call in execute_calls]

        # SAVEPOINT was issued before the context SELECT.
        assert "SAVEPOINT supersede_ctx" in sql_stmts
        # ROLLBACK was issued because the SELECT raised.
        assert "ROLLBACK TO SAVEPOINT supersede_ctx" in sql_stmts
        # Primary supersede still ran.
        assert any("DELETE FROM rulings" in s for s in sql_stmts), (
            "DELETE FROM rulings must run even when context SELECT raises."
        )
        assert any("UPDATE documents" in s and "superseded" in s for s in sql_stmts), (
            "UPDATE documents … superseded must run even when context SELECT raises."
        )

    def test_supersede_metric_insert_failure_does_not_abort_supersede(self) -> None:
        """A failing metric INSERT must not abort the supersede — SAVEPOINT protects it.

        When ``cur.execute`` raises on the ``data_quality_metrics`` INSERT, the
        outer UPDATE documents must already have run (it precedes the metric write),
        and ``ROLLBACK TO SAVEPOINT supersede_metric`` must have been issued.
        """
        import psycopg.errors

        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        unique_exc = psycopg.errors.UniqueViolation(
            "duplicate key value violates unique constraint"
        )
        metric_exc = Exception("metric insert failure")

        def side_effect_execute(sql: str, params: tuple | None = None) -> None:
            if "INSERT INTO rulings" in sql:
                raise unique_exc
            if "INSERT INTO data_quality_metrics" in sql:
                raise metric_exc

        cur.execute = MagicMock(side_effect=side_effect_execute)
        # Winner lookup returns a row; county lookup returns a county.
        cur.fetchone = MagicMock(
            side_effect=[
                ("winner-doc-id",),
                ("Contra Costa", "ca-contra_costa/some.pdf"),
            ]
        )

        insert_ruling(
            conn,
            document_id="losing-doc",
            case_id="case-1",
            court_id="court-1",
            hearing_date=date(2026, 3, 5),
            ruling_text="Motion GRANTED",
            department="Dept. 1",
        )

        execute_calls = cur.execute.call_args_list
        sql_stmts = [call[0][0] for call in execute_calls]

        # SAVEPOINT was issued before the metric INSERT.
        assert "SAVEPOINT supersede_metric" in sql_stmts
        # ROLLBACK was issued because the INSERT raised.
        assert "ROLLBACK TO SAVEPOINT supersede_metric" in sql_stmts
        # Primary supersede UPDATE ran (precedes metric write).
        assert any("UPDATE documents" in s and "superseded" in s for s in sql_stmts), (
            "UPDATE documents … superseded must have run before metric INSERT attempted."
        )


# ---------------------------------------------------------------------------
# _is_all_caps_title
# ---------------------------------------------------------------------------


class TestIsAllCapsTitle:
    """Tests for _is_all_caps_title helper."""

    def test_all_caps_with_vs(self) -> None:
        assert _is_all_caps_title("SMITH VS JONES") is True

    def test_all_caps_with_v_dot(self) -> None:
        assert _is_all_caps_title("SMITH v. JONES") is True

    def test_mixed_case_is_not_all_caps(self) -> None:
        assert _is_all_caps_title("Smith v. Jones") is False

    def test_title_case_is_not_all_caps(self) -> None:
        assert _is_all_caps_title("Current And Former Employees v. Priti Prabha") is False

    def test_long_all_caps(self) -> None:
        assert _is_all_caps_title("CURRENT AND FORMER AGGRIEVED EMPLOYEES vs PRITI PRABHA") is True

    def test_empty_string(self) -> None:
        assert _is_all_caps_title("") is False

    def test_all_caps_with_punctuation(self) -> None:
        assert _is_all_caps_title("ACME, LLC vs DOE") is True


# ---------------------------------------------------------------------------
# normalize_case_title
# ---------------------------------------------------------------------------


class TestNormalizeCaseTitle:
    """Tests for normalize_case_title function."""

    def test_none_returns_none(self) -> None:
        assert normalize_case_title(None) is None

    def test_empty_string_returns_none(self) -> None:
        assert normalize_case_title("") is None

    def test_whitespace_only_returns_none(self) -> None:
        assert normalize_case_title("   ") is None

    def test_all_caps_normalized_to_title_case(self) -> None:
        result = normalize_case_title("SMITH VS JONES")
        assert result == "Smith v. Jones"

    def test_all_caps_long_title(self) -> None:
        result = normalize_case_title("CURRENT AND FORMER AGGRIEVED EMPLOYEES vs PRITI PRABHA")
        assert result == "Current And Former Aggrieved Employees v. Priti Prabha"

    def test_preserves_llc(self) -> None:
        result = normalize_case_title("ACME LLC VS DOE")
        assert result == "Acme LLC v. Doe"

    def test_preserves_llp(self) -> None:
        result = normalize_case_title("SMITH LLP VS JONES")
        assert result == "Smith LLP v. Jones"

    def test_preserves_dba(self) -> None:
        result = normalize_case_title("DOE DBA ACME VS SMITH")
        assert result == "Doe DBA Acme v. Smith"

    def test_preserves_inc(self) -> None:
        result = normalize_case_title("ACME INC VS DOE")
        assert result == "Acme Inc. v. Doe"

    def test_preserves_corp(self) -> None:
        result = normalize_case_title("ACME CORP VS DOE")
        assert result == "Acme Corp. v. Doe"

    def test_preserves_ltd(self) -> None:
        result = normalize_case_title("ACME LTD VS DOE")
        assert result == "Acme Ltd. v. Doe"

    def test_preserves_lp(self) -> None:
        result = normalize_case_title("ACME LP VS DOE")
        assert result == "Acme LP v. Doe"

    def test_preserves_na(self) -> None:
        result = normalize_case_title("BANK N.A. VS DOE")
        assert result == "Bank N.A. v. Doe"

    def test_preserves_pc(self) -> None:
        result = normalize_case_title("LAW FIRM PC VS DOE")
        assert result == "Law Firm PC v. Doe"

    def test_preserves_pllc(self) -> None:
        result = normalize_case_title("LAW FIRM PLLC VS DOE")
        assert result == "Law Firm PLLC v. Doe"

    def test_mixed_case_unchanged(self) -> None:
        """Mixed-case titles should not be modified."""
        title = "Smith v. Jones"
        assert normalize_case_title(title) == title

    def test_title_case_unchanged(self) -> None:
        """Already title-cased titles should not be modified."""
        title = "Current And Former Employees v. Priti Prabha"
        assert normalize_case_title(title) == title

    def test_normalizes_vs_separator(self) -> None:
        """'VS' should become 'v.' in the normalized output."""
        result = normalize_case_title("SMITH VS JONES")
        assert " v. " in result

    def test_normalizes_vs_dot_separator(self) -> None:
        """'VS.' should become 'v.' in the normalized output."""
        result = normalize_case_title("SMITH VS. JONES")
        assert " v. " in result

    def test_whitespace_collapsed(self) -> None:
        result = normalize_case_title("SMITH   VS   JONES")
        assert result == "Smith v. Jones"

    def test_real_example_hussnain(self) -> None:
        result = normalize_case_title("SYED HUSSNAIN v. FORD MOTOR CO.")
        assert result == "Syed Hussnain v. Ford Motor Co."

    def test_preserves_multiple_acronyms(self) -> None:
        result = normalize_case_title("ACME LLC DBA WIDGETS VS DOE CORP")
        assert result == "Acme LLC DBA Widgets v. Doe Corp."

    def test_no_separator_all_caps(self) -> None:
        """A title without v./vs. that is all caps should still be normalized."""
        result = normalize_case_title("SMITH AND JONES")
        assert result == "Smith And Jones"

    def test_newline_in_title_stripped(self) -> None:
        """Literal newline characters should be replaced with a single space."""
        result = normalize_case_title("Husain\nv. McDonald")
        assert result == "Husain v. McDonald"

    def test_carriage_return_in_title_stripped(self) -> None:
        """Carriage return characters should be replaced with a single space."""
        result = normalize_case_title("Husain\r\nv. McDonald")
        assert result == "Husain v. McDonald"

    def test_tab_in_title_stripped(self) -> None:
        """Tab characters should be replaced with a single space."""
        result = normalize_case_title("Husain\tv. McDonald")
        assert result == "Husain v. McDonald"

    def test_multiple_newlines_collapsed(self) -> None:
        """Multiple newlines should be collapsed to a single space."""
        result = normalize_case_title("Smith\n\nv.\n\nJones")
        assert result == "Smith v. Jones"

    def test_upsert_case_applies_normalization(self) -> None:
        """upsert_case should normalize ALL CAPS case titles."""
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = ("case-id-1",)

        upsert_case(conn, "24CV123456", "court-1", case_title="SMITH VS JONES")

        # The SQL params should contain the normalized title
        call_args = cur.execute.call_args[0][1]
        # case_title is the 4th parameter (index 3)
        assert call_args[3] == "Smith v. Jones"


# ---------------------------------------------------------------------------
# insert_document_and_ruling — shared helper (#1790)
# ---------------------------------------------------------------------------


class TestInsertDocumentAndRuling:
    """Tests for the insert_document_and_ruling helper that wraps
    insert_document + insert_ruling with a consistent document_id."""

    def test_calls_insert_document_with_correct_document_id(self) -> None:
        """The helper passes document_id to insert_document."""
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = (True,)  # is_new = True

        insert_document_and_ruling(
            conn,
            document_id="doc-123",
            case_id="case-1",
            court_id="court-1",
            content_format="html",
            content_hash="hash-abc",
            s3_key="rulings/doc.html",
            s3_bucket="bucket-1",
            source_url="https://example.com",
            scraper_id="scraper-1",
            captured_at=datetime(2026, 3, 5, 10, 0, 0),
            hearing_date=date(2026, 3, 10),
            ruling_text="The motion is granted.",
            department="Dept 42",
        )

        # The first execute call (with params) is insert_document.
        # Verify the first param (document_id) is correct.
        calls_with_params = [c for c in cur.execute.call_args_list if len(c[0]) > 1]
        assert len(calls_with_params) >= 1
        # insert_document: first param is document_id
        assert calls_with_params[0][0][1][0] == "doc-123"

    def test_calls_insert_ruling_with_same_document_id(self) -> None:
        """The helper passes the same document_id to both insert_document
        and insert_ruling, preventing FK divergence (#1775)."""
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = (True,)

        insert_document_and_ruling(
            conn,
            document_id="doc-456",
            case_id="case-2",
            court_id="court-2",
            content_format="pdf",
            content_hash="hash-xyz",
            s3_key="rulings/doc.pdf",
            s3_bucket="bucket-2",
            source_url="https://example.com/ruling.pdf",
            scraper_id="scraper-oc",
            captured_at=datetime(2026, 3, 5),
            hearing_date=date(2026, 3, 10),
            ruling_text="Motion denied.",
            department="Dept 5",
            judge_id="judge-1",
            outcome="denied",
            motion_type="Motion to Compel",
        )

        # Collect all execute calls with params (skip SAVEPOINT/RELEASE).
        calls_with_params = [c for c in cur.execute.call_args_list if len(c[0]) > 1]
        # At minimum: insert_document (1 call) + insert_ruling (1 call)
        assert len(calls_with_params) >= 2

        # Both should have "doc-456" as first param (document_id)
        insert_doc_params = calls_with_params[0][0][1]
        insert_ruling_params = calls_with_params[1][0][1]
        assert insert_doc_params[0] == "doc-456"
        assert insert_ruling_params[0] == "doc-456"

    def test_inserts_ruling_even_when_hearing_date_is_none(self) -> None:
        """When hearing_date is None, both document and ruling are inserted (#2215).

        Prior to #2215, the ruling was silently skipped when hearing_date was
        None.  Now the ruling is always inserted, with hearing_date=NULL.
        """
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = (True,)

        result = insert_document_and_ruling(
            conn,
            document_id="doc-no-hearing",
            case_id="case-3",
            court_id="court-3",
            content_format="html",
            content_hash="hash-noh",
            s3_key=None,
            s3_bucket=None,
            source_url="https://example.com",
            scraper_id="scraper-1",
            captured_at=datetime(2026, 3, 5),
            hearing_date=None,
            ruling_text="Some text",
        )

        assert result is True
        # Both insert_document AND insert_ruling should have been called.
        all_sql = " ".join(str(c) for c in cur.execute.call_args_list)
        assert "INSERT INTO documents" in all_sql
        assert "INSERT INTO rulings" in all_sql

    def test_returns_is_new_true_for_new_document(self) -> None:
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = (True,)

        result = insert_document_and_ruling(
            conn,
            document_id="doc-new",
            case_id="case-4",
            court_id="court-4",
            content_format="html",
            content_hash="hash-new",
            s3_key="rulings/new.html",
            s3_bucket="bucket-1",
            source_url="https://example.com",
            scraper_id="scraper-1",
            captured_at=datetime(2026, 3, 5),
            hearing_date=date(2026, 3, 10),
            ruling_text="Granted.",
        )
        assert result is True

    def test_returns_is_new_false_for_existing_document(self) -> None:
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = (False,)

        result = insert_document_and_ruling(
            conn,
            document_id="doc-existing",
            case_id="case-5",
            court_id="court-5",
            content_format="pdf",
            content_hash="hash-exist",
            s3_key="rulings/exist.pdf",
            s3_bucket="bucket-1",
            source_url="https://example.com",
            scraper_id="scraper-1",
            captured_at=datetime(2026, 3, 5),
            hearing_date=date(2026, 3, 10),
            ruling_text="Denied.",
        )
        assert result is False

    def test_passes_all_ruling_fields(self) -> None:
        """All optional ruling fields (ruling_text_html, summary, etc.)
        are forwarded to insert_ruling."""
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = (True,)

        summary_ts = datetime(2026, 3, 5, 12, 0, 0)
        insert_document_and_ruling(
            conn,
            document_id="doc-full",
            case_id="case-6",
            court_id="court-6",
            content_format="html",
            content_hash="hash-full",
            s3_key="rulings/full.html",
            s3_bucket="bucket-1",
            source_url="https://example.com",
            scraper_id="scraper-1",
            captured_at=datetime(2026, 3, 5),
            hearing_date=date(2026, 3, 10),
            ruling_text="The motion is granted.",
            ruling_text_html="<p>The motion is granted.</p>",
            department="Dept 42",
            judge_id="judge-1",
            outcome="granted",
            motion_type="Demurrer",
            summary="Motion was granted.",
            summary_model="gemini-2.0-flash",
            summary_generated_at=summary_ts,
        )

        # Find the insert_ruling SQL call (contains 'INSERT INTO rulings').
        ruling_calls = [
            c
            for c in cur.execute.call_args_list
            if len(c[0]) > 0 and "INSERT INTO rulings" in str(c[0][0])
        ]
        assert len(ruling_calls) == 1
        ruling_params = ruling_calls[0][0][1]
        # ruling params: (document_id, case_id, court_id, judge_id,
        #                 hearing_date, ruling_text, ruling_text_html,
        #                 department, outcome, motion_type,
        #                 summary, summary_model, summary_generated_at,
        #                 text_hash)
        assert ruling_params[0] == "doc-full"  # document_id
        assert ruling_params[6] == "<p>The motion is granted.</p>"  # ruling_text_html
        assert ruling_params[10] == "Motion was granted."  # summary
        assert ruling_params[11] == "gemini-2.0-flash"  # summary_model
        assert ruling_params[12] == summary_ts  # summary_generated_at


# ---------------------------------------------------------------------------
# lookup_existing_case_title (#2006)
# ---------------------------------------------------------------------------


class TestLookupExistingCaseTitle:
    """Unit tests for the lookup_existing_case_title function."""

    def test_returns_title_when_case_exists(self) -> None:
        """Should return the case_title when a matching case exists in the DB."""
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = ("Smith v. Jones",)

        result = lookup_existing_case_title(conn, "24STCV12345", "court-uuid-1")

        assert result == "Smith v. Jones"
        cur.execute.assert_called_once()
        params = cur.execute.call_args[0][1]
        assert params == ("court-uuid-1", "24STCV12345")

    def test_returns_none_when_case_not_found(self) -> None:
        """Should return None when no matching case exists in the DB."""
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = None

        result = lookup_existing_case_title(conn, "NOSUCH123", "court-uuid-1")

        assert result is None

    def test_returns_none_when_title_is_null(self) -> None:
        """Should return None when the case exists but has a null title."""
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = (None,)

        result = lookup_existing_case_title(conn, "24STCV12345", "court-uuid-1")

        assert result is None


# ---------------------------------------------------------------------------
# upsert_case_returning_title (#2006)
# ---------------------------------------------------------------------------


class TestUpsertCaseReturningTitle:
    """Unit tests for the upsert_case_returning_title function."""

    def test_returns_id_and_title(self) -> None:
        """Should return both the case UUID and the effective case_title."""
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = ("case-uuid-1", "Smith v. Jones")

        case_id, title = upsert_case_returning_title(
            conn, "24STCV12345", "court-uuid-1", case_title="Smith v. Jones"
        )

        assert case_id == "case-uuid-1"
        assert title == "Smith v. Jones"

    def test_returns_existing_title_on_conflict(self) -> None:
        """When inserting with null title, COALESCE preserves the existing title."""
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        # Simulate: inserted with null title, DB returned existing title
        cur.fetchone.return_value = ("case-uuid-1", "Existing Title v. Defendant")

        case_id, title = upsert_case_returning_title(
            conn, "24STCV12345", "court-uuid-1", case_title=None
        )

        assert case_id == "case-uuid-1"
        assert title == "Existing Title v. Defendant"

    def test_returns_none_title_when_no_title_exists(self) -> None:
        """When no title exists and none provided, effective_title is None."""
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = ("case-uuid-1", None)

        case_id, title = upsert_case_returning_title(
            conn, "24STCV12345", "court-uuid-1", case_title=None
        )

        assert case_id == "case-uuid-1"
        assert title is None

    def test_handles_single_column_return(self) -> None:
        """Gracefully handles RETURNING with only one column (backwards compat)."""
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = ("case-uuid-1",)

        case_id, title = upsert_case_returning_title(conn, "24STCV12345", "court-uuid-1")

        assert case_id == "case-uuid-1"
        assert title is None

    def test_raises_on_no_row(self) -> None:
        """Should raise RuntimeError when fetchone returns None."""
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = None

        with pytest.raises(RuntimeError, match="could not retrieve case id"):
            upsert_case_returning_title(conn, "24STCV12345", "court-uuid-1")


# ---------------------------------------------------------------------------
# upsert_case / upsert_case_returning_title — preserve-on-conflict SQL (#2468)
# ---------------------------------------------------------------------------


def _upsert_case_execute_sql(conn: MagicMock) -> str:
    """Return the SQL string passed to the last cursor.execute() call.

    Used to assert the ON CONFLICT clause structure in upsert_case /
    upsert_case_returning_title.
    """
    cur = conn.cursor.return_value.__enter__.return_value
    for call in reversed(cur.execute.call_args_list):
        args = call[0]
        if args and isinstance(args[0], str) and "INSERT INTO cases" in args[0]:
            return args[0]
    raise ValueError("No execute() call with INSERT INTO cases found")


class TestUpsertCasePreservesExistingTitle:
    """Regression tests for #2468 — ensure non-null existing case_title is
    never silently overwritten by a later upsert.

    The bug was that ``COALESCE(EXCLUDED.case_title, cases.case_title)`` used
    the incoming value whenever it was non-null, so any upstream mis-routing
    (e.g. fuzzy-match case-number rewrites, #2449) would propagate wrong
    titles across the DB.  The fix swaps the COALESCE argument order to
    ``COALESCE(cases.case_title, EXCLUDED.case_title)`` so the existing
    value wins when present; the incoming value only fills in a currently
    NULL column.
    """

    def test_upsert_case_sql_preserves_existing_title(self) -> None:
        """upsert_case SQL must COALESCE(cases.case_title, EXCLUDED.case_title)."""
        conn = _mock_conn()
        upsert_case(conn, "24STCV12345", "court-uuid-1", case_title="New Title")

        sql = _upsert_case_execute_sql(conn)
        assert "case_title = COALESCE(cases.case_title, EXCLUDED.case_title)" in sql, (
            f"Expected preserve-first COALESCE for case_title, got SQL:\n{sql}"
        )
        # The old buggy order must not appear.
        assert "COALESCE(EXCLUDED.case_title" not in sql, (
            f"Legacy overwrite COALESCE order found — this is the #2468 bug. SQL:\n{sql}"
        )

    def test_upsert_case_sql_preserves_existing_case_type(self) -> None:
        """upsert_case SQL must COALESCE(cases.case_type, EXCLUDED.case_type)."""
        conn = _mock_conn()
        upsert_case(
            conn,
            "24STCV12345",
            "court-uuid-1",
            case_title="Title",
            case_type="civil",
        )

        sql = _upsert_case_execute_sql(conn)
        assert (
            "case_type  = COALESCE(cases.case_type, EXCLUDED.case_type)" in sql
            or "case_type = COALESCE(cases.case_type, EXCLUDED.case_type)" in sql
        ), f"Expected preserve-first COALESCE for case_type, got SQL:\n{sql}"
        assert "COALESCE(EXCLUDED.case_type" not in sql, (
            f"Legacy overwrite COALESCE order found for case_type. SQL:\n{sql}"
        )

    def test_upsert_case_returning_title_sql_preserves_existing_title(
        self,
    ) -> None:
        """upsert_case_returning_title SQL must preserve existing case_title."""
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = ("case-uuid-1", "Csicsery Family Trust")

        upsert_case_returning_title(
            conn,
            "P25-02101",
            "court-uuid-1",
            case_title="Vincent Revocable Trust",
        )

        sql = _upsert_case_execute_sql(conn)
        assert "case_title = COALESCE(cases.case_title, EXCLUDED.case_title)" in sql, (
            f"Expected preserve-first COALESCE for case_title, got SQL:\n{sql}"
        )
        assert "COALESCE(EXCLUDED.case_title" not in sql, (
            f"Legacy overwrite COALESCE order found — this is the #2468 bug. SQL:\n{sql}"
        )

    def test_upsert_case_returning_title_sql_preserves_existing_case_type(
        self,
    ) -> None:
        """upsert_case_returning_title SQL must preserve existing case_type."""
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = ("case-uuid-1", "Existing Title")

        upsert_case_returning_title(
            conn,
            "P25-02101",
            "court-uuid-1",
            case_title=None,
            case_type="probate",
        )

        sql = _upsert_case_execute_sql(conn)
        assert (
            "case_type  = COALESCE(cases.case_type, EXCLUDED.case_type)" in sql
            or "case_type = COALESCE(cases.case_type, EXCLUDED.case_type)" in sql
        ), f"Expected preserve-first COALESCE for case_type, got SQL:\n{sql}"
        assert "COALESCE(EXCLUDED.case_type" not in sql, (
            f"Legacy overwrite COALESCE order found for case_type. SQL:\n{sql}"
        )

    def test_csicsery_vs_vincent_scenario_preserves_first_title(self) -> None:
        """Scenario from the issue: P25-02101 is Csicsery, not Vincent.

        When Vincent's LLM-returned ruling (fuzzy-match-mis-routed onto
        P25-02101) calls upsert_case_returning_title with title "Vincent
        Revocable Trust", the DB row for P25-02101 must still return the
        Csicsery title — i.e. the DB's existing title wins over the
        incoming non-null value.

        This test mocks the DB behavior: after swapping the COALESCE
        argument order, a conflict returns the preserved existing title
        from RETURNING.  Asserts that the caller receives the preserved
        title, which is the whole point of #2468.
        """
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        # Simulate the DB after the fix: on conflict with an existing
        # non-null case_title, RETURNING case_title returns the existing
        # (preserved) value, not the incoming one.
        cur.fetchone.return_value = (
            "case-uuid-csicsery",
            "Matter of the Csicsery Family Trust",
        )

        case_id, effective_title = upsert_case_returning_title(
            conn,
            "P25-02101",
            "court-uuid-contra-costa",
            case_title="Matter of: The George R. Vincent Revocable Trust",
        )

        assert case_id == "case-uuid-csicsery"
        assert effective_title == "Matter of the Csicsery Family Trust"
        # And the SQL still encodes the preserve-existing semantics.
        sql = _upsert_case_execute_sql(conn)
        assert "case_title = COALESCE(cases.case_title, EXCLUDED.case_title)" in sql

    def test_upsert_case_fill_in_null_title_still_works(self) -> None:
        """Cross-case title lookup (#2006) must still work.

        If a case row exists with case_title = NULL and a later upsert
        provides a non-null title, the incoming title should fill in the
        null column.  ``COALESCE(cases.case_title, EXCLUDED.case_title)``
        preserves this: when the existing value is NULL, the incoming
        value wins.
        """
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        # Simulate an existing row with null case_title — the DB's
        # COALESCE(cases.case_title, EXCLUDED.case_title) will return the
        # incoming EXCLUDED value.
        cur.fetchone.return_value = ("case-uuid-1", "Newly Filled In Title")

        case_id, effective_title = upsert_case_returning_title(
            conn,
            "24STCV12345",
            "court-uuid-1",
            case_title="Newly Filled In Title",
        )

        assert case_id == "case-uuid-1"
        assert effective_title == "Newly Filled In Title"


class TestUpsertCaseForceUpdate:
    """Regression tests for #2431 — ``force_update=True`` must allow
    reingest to correct previously-stuck ``case_type`` / ``case_title``
    values after an extraction logic fix.

    The #2468 fix swapped the COALESCE argument order so the existing
    value wins on conflict, preventing silent identity rewrites from
    upstream mis-routing bugs (#2449).  That made reingest unable to
    fix legitimately stuck values (e.g. an SF document misclassified as
    ``case_type='criminal'`` that should be ``'family'`` after the #2368
    regex fix).  ``force_update=True`` restores the overwrite behavior
    for reingest while preserving the safe preserve-existing default for
    live ingestion, and NULL incoming values still never erase a good
    existing column (COALESCE falls through to ``cases.*``).
    """

    def test_upsert_case_force_update_flips_case_title_coalesce(self) -> None:
        """``upsert_case(force_update=True)`` must use
        ``COALESCE(EXCLUDED.case_title, cases.case_title)`` so the
        incoming value wins when non-NULL."""
        conn = _mock_conn()
        upsert_case(
            conn,
            "24STCV12345",
            "court-uuid-1",
            case_title="Corrected Title",
            force_update=True,
        )

        sql = _upsert_case_execute_sql(conn)
        assert "case_title = COALESCE(EXCLUDED.case_title, cases.case_title)" in sql, (
            f"Expected incoming-wins COALESCE for case_title in force_update mode, got SQL:\n{sql}"
        )
        # Preserve-existing order must NOT appear in force_update mode.
        assert "COALESCE(cases.case_title" not in sql, (
            f"Preserve-existing COALESCE order found in force_update mode. SQL:\n{sql}"
        )

    def test_upsert_case_force_update_flips_case_type_coalesce(self) -> None:
        """``upsert_case(force_update=True)`` must use
        ``COALESCE(EXCLUDED.case_type, cases.case_type)`` so the
        incoming value wins when non-NULL."""
        conn = _mock_conn()
        upsert_case(
            conn,
            "24STCV12345",
            "court-uuid-1",
            case_title="Title",
            case_type="family",
            force_update=True,
        )

        sql = _upsert_case_execute_sql(conn)
        assert (
            "case_type  = COALESCE(EXCLUDED.case_type, cases.case_type)" in sql
            or "case_type = COALESCE(EXCLUDED.case_type, cases.case_type)" in sql
        ), f"Expected incoming-wins COALESCE for case_type in force_update mode, got SQL:\n{sql}"
        assert "COALESCE(cases.case_type" not in sql, (
            "Preserve-existing COALESCE order found for case_type in force_update mode. "
            f"SQL:\n{sql}"
        )

    def test_upsert_case_default_keeps_preserve_existing_coalesce(self) -> None:
        """Without ``force_update``, ``upsert_case`` MUST keep the
        preserve-existing COALESCE order (#2468).  Explicit assertion that
        the default and force_update modes yield different SQL."""
        conn = _mock_conn()
        upsert_case(
            conn,
            "24STCV12345",
            "court-uuid-1",
            case_title="Incoming Title",
            case_type="civil",
        )

        sql = _upsert_case_execute_sql(conn)
        assert "case_title = COALESCE(cases.case_title, EXCLUDED.case_title)" in sql
        assert (
            "case_type  = COALESCE(cases.case_type, EXCLUDED.case_type)" in sql
            or "case_type = COALESCE(cases.case_type, EXCLUDED.case_type)" in sql
        )
        # The force_update variant must NOT appear in the default-mode SQL.
        assert "COALESCE(EXCLUDED.case_title" not in sql
        assert "COALESCE(EXCLUDED.case_type" not in sql

    def test_upsert_case_returning_title_force_update_flips_case_title(
        self,
    ) -> None:
        """``upsert_case_returning_title(force_update=True)`` must use
        ``COALESCE(EXCLUDED.case_title, cases.case_title)``."""
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = ("case-uuid-1", "Corrected Title")

        case_id, effective_title = upsert_case_returning_title(
            conn,
            "24STCV12345",
            "court-uuid-1",
            case_title="Corrected Title",
            force_update=True,
        )

        assert case_id == "case-uuid-1"
        assert effective_title == "Corrected Title"
        sql = _upsert_case_execute_sql(conn)
        assert "case_title = COALESCE(EXCLUDED.case_title, cases.case_title)" in sql
        assert "COALESCE(cases.case_title" not in sql

    def test_upsert_case_returning_title_force_update_flips_case_type(self) -> None:
        """``upsert_case_returning_title(force_update=True)`` must use
        ``COALESCE(EXCLUDED.case_type, cases.case_type)``."""
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = ("case-uuid-1", "Title")

        upsert_case_returning_title(
            conn,
            "FDI-12-345678",
            "court-uuid-sf",
            case_title=None,
            case_type="family",
            force_update=True,
        )

        sql = _upsert_case_execute_sql(conn)
        assert (
            "case_type  = COALESCE(EXCLUDED.case_type, cases.case_type)" in sql
            or "case_type = COALESCE(EXCLUDED.case_type, cases.case_type)" in sql
        )
        assert "COALESCE(cases.case_type" not in sql

    def test_upsert_case_returning_title_default_keeps_preserve_existing(
        self,
    ) -> None:
        """Without ``force_update``, ``upsert_case_returning_title`` MUST
        keep the preserve-existing COALESCE order (#2468)."""
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = ("case-uuid-1", "Existing Title")

        upsert_case_returning_title(
            conn,
            "24STCV12345",
            "court-uuid-1",
            case_title="Incoming Title",
            case_type="civil",
        )

        sql = _upsert_case_execute_sql(conn)
        assert "case_title = COALESCE(cases.case_title, EXCLUDED.case_title)" in sql
        assert "COALESCE(EXCLUDED.case_title" not in sql

    def test_sf_family_reingest_scenario_corrects_case_type(self) -> None:
        """End-to-end-ish scenario from #2431: an SF family-court case
        row has ``case_type='criminal'`` from a pre-fix extraction pass.
        After the #2368 regex fix, reingest passes ``case_type='family'``
        with ``force_update=True``.  The SQL must encode the incoming-wins
        COALESCE so Postgres will write the corrected value.  The mocked
        RETURNING returns the corrected effective type so callers see
        the new value on subsequent reads."""
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        # Simulate Postgres behavior after the force_update fix: on conflict
        # with an existing non-null case_type, COALESCE(EXCLUDED.case_type,
        # cases.case_type) picks the incoming 'family' value and RETURNING
        # returns the corrected type via case_title (we only fetch id here,
        # but the SQL is the source of truth).
        cur.fetchone.return_value = ("case-uuid-sf-1",)

        case_id = upsert_case(
            conn,
            "FDI-12-345678",
            "court-uuid-sf",
            case_title="IN RE MARRIAGE OF SMITH",
            case_type="family",
            force_update=True,
        )

        assert case_id == "case-uuid-sf-1"
        sql = _upsert_case_execute_sql(conn)
        # Both columns must use the incoming-wins order.
        assert "case_title = COALESCE(EXCLUDED.case_title, cases.case_title)" in sql
        assert (
            "case_type  = COALESCE(EXCLUDED.case_type, cases.case_type)" in sql
            or "case_type = COALESCE(EXCLUDED.case_type, cases.case_type)" in sql
        )
        # And the incoming values were actually passed as parameters.
        args = _get_execute_args(conn)
        assert "family" in args
        # case_title is normalized (title-cased); verify the normalized
        # form is what lands in the parameter list.
        assert any(
            isinstance(a, str) and a.lower().startswith("in re marriage of smith") for a in args
        )

    def test_upsert_case_force_update_null_incoming_never_erases_existing(
        self,
    ) -> None:
        """Even with ``force_update=True``, a NULL incoming value must
        not erase a good existing column.  The SQL
        ``COALESCE(EXCLUDED.case_title, cases.case_title)`` naturally
        preserves this: when EXCLUDED is NULL, COALESCE falls through to
        ``cases.case_title``.  This test asserts that the SQL is
        structured that way (not e.g. the raw ``EXCLUDED.case_title``
        which would erase).
        """
        conn = _mock_conn()
        upsert_case(
            conn,
            "24STCV12345",
            "court-uuid-1",
            case_title=None,
            case_type=None,
            force_update=True,
        )
        sql = _upsert_case_execute_sql(conn)
        # Both columns must still wrap EXCLUDED in COALESCE against cases.*
        # so NULL EXCLUDED falls back to the preserved existing value.
        assert "case_title = COALESCE(EXCLUDED.case_title, cases.case_title)" in sql
        assert (
            "case_type  = COALESCE(EXCLUDED.case_type, cases.case_type)" in sql
            or "case_type = COALESCE(EXCLUDED.case_type, cases.case_type)" in sql
        )
        # Guard against a regression that would write EXCLUDED.* raw.
        assert "case_title = EXCLUDED.case_title" not in sql
        assert "case_type = EXCLUDED.case_type" not in sql


# ---------------------------------------------------------------------------
# resolve_judge_from_department
# ---------------------------------------------------------------------------


class TestResolveJudgeFromDepartment:
    """Tests for resolve_judge_from_department function (#2269)."""

    def test_returns_judge_from_snapshot_exact_dept_match(self) -> None:
        """Should return judge name when department matches exactly in snapshot."""
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        # First fetchone: court_code lookup
        # Second fetchone: snapshot mapping lookup
        mapping = {"3": "John Smith", "5": "Jane Doe"}
        cur.fetchone.side_effect = [("ca-los-angeles",), (mapping,)]

        result = resolve_judge_from_department(conn, "court-uuid-1", "3")
        assert result == "John Smith"

    def test_returns_judge_case_insensitive_dept_match(self) -> None:
        """Should return judge name when department matches case-insensitively."""
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        mapping = {"Dept 3": "John Smith"}
        cur.fetchone.side_effect = [("ca-los-angeles",), (mapping,)]

        result = resolve_judge_from_department(conn, "court-uuid-1", "dept 3")
        assert result == "John Smith"

    def test_returns_none_when_no_court(self) -> None:
        """Should return None when court_id doesn't exist."""
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = None

        result = resolve_judge_from_department(conn, "nonexistent-court", "3")
        assert result is None

    def test_returns_none_when_no_snapshot(self) -> None:
        """Should return None when no directory snapshot exists."""
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        # First: court_code, Second: no snapshot
        cur.fetchone.side_effect = [("ca-los-angeles",), None]

        result = resolve_judge_from_department(conn, "court-uuid-1", "3")
        assert result is None

    def test_returns_none_when_dept_not_in_mapping(self) -> None:
        """Should return None when department is not in the mapping."""
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        mapping = {"5": "Jane Doe"}
        cur.fetchone.side_effect = [("ca-los-angeles",), (mapping,)]

        result = resolve_judge_from_department(conn, "court-uuid-1", "3")
        assert result is None

    def test_uses_hearing_date_for_historical_lookup(self) -> None:
        """Should use hearing_date to select appropriate historical snapshot."""
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        mapping = {"3": "John Smith"}
        cur.fetchone.side_effect = [("ca-ventura",), (mapping,)]
        hearing_dt = date(2026, 1, 15)

        result = resolve_judge_from_department(conn, "court-uuid-1", "3", hearing_date=hearing_dt)
        assert result == "John Smith"

        # Verify the snapshot query includes the hearing date
        snapshot_query_call = cur.execute.call_args_list[1]
        sql = snapshot_query_call[0][0]
        assert "captured_at <=" in sql
        params = snapshot_query_call[0][1]
        assert hearing_dt in params

    def test_converts_court_code_hyphens_to_underscores(self) -> None:
        """Should convert court_code hyphens to underscores for snapshot lookup."""
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        mapping = {"3": "John Smith"}
        cur.fetchone.side_effect = [("ca-los-angeles",), (mapping,)]

        resolve_judge_from_department(conn, "court-uuid-1", "3")

        # Verify the snapshot query uses underscored court_id
        snapshot_query_call = cur.execute.call_args_list[1]
        params = snapshot_query_call[0][1]
        assert "ca_los_angeles" in params

    def test_handles_json_string_mapping(self) -> None:
        """Should handle mapping stored as JSON string (not dict)."""
        import json

        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        mapping_str = json.dumps({"3": "John Smith"})
        cur.fetchone.side_effect = [("ca-ventura",), (mapping_str,)]

        result = resolve_judge_from_department(conn, "court-uuid-1", "3")
        assert result == "John Smith"

    def test_handles_null_mapping_data(self) -> None:
        """Should return None when snapshot mapping is NULL."""
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.side_effect = [("ca-los-angeles",), (None,)]

        result = resolve_judge_from_department(conn, "court-uuid-1", "3")
        assert result is None

    def test_falls_back_to_earliest_snapshot_when_hearing_date_predates_all(
        self,
    ) -> None:
        """When hearing_date predates all snapshots, fall back to earliest snapshot.

        Regression: #2602 — Ventura J6 probate rulings (and numeric depts) were
        NULL-judge because their hearing_date predated the oldest court directory
        snapshot (2026-03-31). The <= hearing_date query returned no row, and
        the function returned None with no fallback.

        Judicial assignments change infrequently, so the earliest snapshot is
        the best available approximation for pre-snapshot-window hearings.
        """
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        # Call sequence:
        #   1. court_code lookup -> ("ca-ventura",)
        #   2. historical snapshot (<= hearing_date) -> None (no row)
        #   3. earliest snapshot fallback (ASC LIMIT 1) -> ({"J6": "..."},)
        earliest_mapping = {"J6": "Gilbert A. Romero", "20": "Ronda J. McKaig"}
        cur.fetchone.side_effect = [
            ("ca-ventura",),
            None,
            (earliest_mapping,),
        ]
        hearing_dt = date(2026, 3, 17)

        result = resolve_judge_from_department(conn, "court-uuid-1", "J6", hearing_date=hearing_dt)
        assert result == "Gilbert A. Romero"

        # Verify the fallback query is ORDER BY captured_at ASC LIMIT 1
        assert len(cur.execute.call_args_list) == 3
        fallback_call = cur.execute.call_args_list[2]
        fallback_sql = fallback_call[0][0]
        assert "ORDER BY captured_at ASC" in fallback_sql
        assert "LIMIT 1" in fallback_sql
        fallback_params = fallback_call[0][1]
        assert "ca_ventura" in fallback_params

    def test_returns_none_when_no_snapshots_at_all_with_hearing_date(self) -> None:
        """When hearing_date is set and no snapshots exist at all, return None.

        The historical-snapshot query returns None, the earliest-snapshot
        fallback also returns None, and the function returns None.
        """
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.side_effect = [
            ("ca-ventura",),
            None,  # historical snapshot: no row
            None,  # earliest snapshot fallback: no row either
        ]
        hearing_dt = date(2026, 3, 17)

        result = resolve_judge_from_department(conn, "court-uuid-1", "J6", hearing_date=hearing_dt)
        assert result is None

    def test_no_fallback_needed_when_historical_snapshot_exists(self) -> None:
        """When a snapshot predates hearing_date, use it and do NOT run the fallback."""
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        mapping = {"J6": "Gilbert A. Romero"}
        cur.fetchone.side_effect = [("ca-ventura",), (mapping,)]
        hearing_dt = date(2026, 4, 16)

        result = resolve_judge_from_department(conn, "court-uuid-1", "J6", hearing_date=hearing_dt)
        assert result == "Gilbert A. Romero"

        # Only two execute calls: court_code + historical snapshot — no fallback
        assert len(cur.execute.call_args_list) == 2


# ---------------------------------------------------------------------------
# delete_stale_split_children
# ---------------------------------------------------------------------------


class TestDeleteStaleSplitChildren:
    """Unit tests for delete_stale_split_children (#2295)."""

    def test_deletes_stale_split_children(self) -> None:
        """Should delete UUID v5 documents with the same s3_key not in valid set."""
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        # First query returns stale document IDs
        stale_id = "aaaaaaaa-5555-5555-5555-aaaaaaaaaaaa"
        cur.fetchall.return_value = [(stale_id,)]
        # Second batch: rowcount for the DELETE FROM documents
        cur.rowcount = 1

        result = delete_stale_split_children(
            conn,
            s3_key="ca/orange/superior_court/raw/abc123.pdf",
            valid_document_ids=["bbbbbbbb-5555-5555-5555-bbbbbbbbbbbb"],
        )
        assert result == 1

        # Should have executed queries: SELECT stale, DELETE alert_events,
        # DELETE validation_results, DELETE rulings, DELETE documents
        assert cur.execute.call_count == 5

    def test_no_stale_children_returns_zero(self) -> None:
        """Should return 0 and not delete when no stale children found."""
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchall.return_value = []

        result = delete_stale_split_children(
            conn,
            s3_key="ca/orange/superior_court/raw/abc123.pdf",
            valid_document_ids=["bbbbbbbb-5555-5555-5555-bbbbbbbbbbbb"],
        )
        assert result == 0
        # Only the SELECT query should have run
        assert cur.execute.call_count == 1

    def test_empty_s3_key_returns_zero(self) -> None:
        """Should return 0 immediately when s3_key is empty."""
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value

        result = delete_stale_split_children(
            conn,
            s3_key="",
            valid_document_ids=["bbbbbbbb-5555-5555-5555-bbbbbbbbbbbb"],
        )
        assert result == 0
        cur.execute.assert_not_called()

    def test_none_s3_key_returns_zero(self) -> None:
        """Should return 0 immediately when s3_key is None."""
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value

        # Type annotation says str, but runtime callers may pass None
        result = delete_stale_split_children(
            conn,
            s3_key=None,  # type: ignore[arg-type]
            valid_document_ids=[],
        )
        assert result == 0
        cur.execute.assert_not_called()

    def test_cascades_to_dependent_tables(self) -> None:
        """Should delete from alert_events, validation_results, rulings before documents."""
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        stale_id = "cccccccc-5555-5555-5555-cccccccccccc"
        cur.fetchall.return_value = [(stale_id,)]
        cur.rowcount = 1

        delete_stale_split_children(
            conn,
            s3_key="ca/orange/superior_court/raw/abc123.pdf",
            valid_document_ids=[],
        )

        # Extract SQL from execute calls (after the SELECT)
        sql_calls = [call[0][0].strip() for call in cur.execute.call_args_list[1:]]
        assert len(sql_calls) == 4
        assert "alert_events" in sql_calls[0]
        assert "validation_results" in sql_calls[1]
        assert "rulings" in sql_calls[2]
        assert "DELETE FROM documents" in sql_calls[3]


# ---------------------------------------------------------------------------
# insert_ruling / insert_document identity-anchor preservation (#2475)
# ---------------------------------------------------------------------------


def _insert_ruling_conflict_sql(conn: MagicMock) -> str:
    """Extract the ``INSERT INTO rulings`` SQL from a mocked ``insert_ruling`` call.

    Returns the raw SQL string so tests can assert on the ON CONFLICT clause.
    """
    cur = conn.cursor.return_value.__enter__.return_value
    for call in reversed(cur.execute.call_args_list):
        args = call[0]
        if args and isinstance(args[0], str) and "INSERT INTO rulings" in args[0]:
            return args[0]
    raise ValueError("No execute() call with INSERT INTO rulings found")


def _insert_document_sql(conn: MagicMock) -> str:
    """Extract the ``INSERT INTO documents`` SQL from a mocked ``insert_document`` call."""
    cur = conn.cursor.return_value.__enter__.return_value
    for call in reversed(cur.execute.call_args_list):
        args = call[0]
        if args and isinstance(args[0], str) and "INSERT INTO documents" in args[0]:
            return args[0]
    raise ValueError("No execute() call with INSERT INTO documents found")


class TestInsertRulingPreservesIdentityAnchors:
    """Regression tests for #2475 — identity-anchor preservation in ``insert_ruling``.

    ``case_id`` and ``judge_id`` are **identity anchors**: once a ruling has been
    linked to a case and a judge, a later live-ingestion re-ingest must NOT
    silently relink it to a different case or judge.  The pre-#2475 SQL used
    ``COALESCE(EXCLUDED.case_id, rulings.case_id)`` which meant an incoming
    non-NULL value always won — exactly the shape of the #2468 bug.  The fix
    swaps the argument order for these two columns only, so the existing value
    wins in the default (live-ingestion) path.

    ``force_update=True`` still overwrites with ``EXCLUDED.*`` unconditionally
    so ``reingest_from_s3.py`` can correct bad historical identity anchors
    (e.g. after fixing an upstream fuzzy-match bug).
    """

    def test_insert_ruling_default_preserves_case_id(self) -> None:
        """Default mode: ON CONFLICT uses COALESCE(rulings.case_id, EXCLUDED.case_id)."""
        conn = _mock_conn()
        insert_ruling(
            conn,
            document_id="doc-1",
            case_id="case-1",
            court_id="court-1",
            hearing_date=date(2026, 3, 5),
            ruling_text="Motion GRANTED",
            department="Dept. 1",
            judge_id="judge-1",
        )

        sql = _insert_ruling_conflict_sql(conn)
        assert "case_id = COALESCE(rulings.case_id, EXCLUDED.case_id)" in sql, (
            f"Expected preserve-first COALESCE for case_id, got SQL:\n{sql}"
        )
        # The old buggy order must not appear.
        assert "COALESCE(EXCLUDED.case_id" not in sql, (
            f"Legacy overwrite COALESCE order found for case_id — this is the "
            f"#2475/#2468 identity-anchor bug. SQL:\n{sql}"
        )

    def test_insert_ruling_default_preserves_judge_id(self) -> None:
        """Default mode: ON CONFLICT uses COALESCE(rulings.judge_id, EXCLUDED.judge_id)."""
        conn = _mock_conn()
        insert_ruling(
            conn,
            document_id="doc-1",
            case_id="case-1",
            court_id="court-1",
            hearing_date=date(2026, 3, 5),
            ruling_text="Motion GRANTED",
            department="Dept. 1",
            judge_id="judge-1",
        )

        sql = _insert_ruling_conflict_sql(conn)
        assert "judge_id = COALESCE(rulings.judge_id, EXCLUDED.judge_id)" in sql, (
            f"Expected preserve-first COALESCE for judge_id, got SQL:\n{sql}"
        )
        assert "COALESCE(EXCLUDED.judge_id" not in sql, (
            f"Legacy overwrite COALESCE order found for judge_id. SQL:\n{sql}"
        )

    def test_insert_ruling_correctable_fields_still_use_incoming_wins(self) -> None:
        """Correctable facts (outcome, motion_type, department, hearing_date,
        ruling_text, ruling_text_html, summary) keep the incoming-wins
        COALESCE(EXCLUDED.*, rulings.*) order in default mode — a later,
        higher-quality extraction legitimately should replace them.
        """
        conn = _mock_conn()
        insert_ruling(
            conn,
            document_id="doc-1",
            case_id="case-1",
            court_id="court-1",
            hearing_date=date(2026, 3, 5),
            ruling_text="Motion GRANTED",
            department="Dept. 1",
            judge_id="judge-1",
            outcome="granted",
            motion_type="Demurrer",
            ruling_text_html="<p>Motion GRANTED</p>",
            summary="Motion granted.",
        )

        sql = _insert_ruling_conflict_sql(conn)
        # Each correctable field must keep EXCLUDED-first COALESCE semantics.
        expected_correctable_fragments = [
            "hearing_date = COALESCE(EXCLUDED.hearing_date, rulings.hearing_date)",
            "outcome = COALESCE(EXCLUDED.outcome, rulings.outcome)",
            "motion_type = COALESCE(EXCLUDED.motion_type, rulings.motion_type)",
            "department = COALESCE(EXCLUDED.department, rulings.department)",
            "ruling_text = COALESCE(EXCLUDED.ruling_text, rulings.ruling_text)",
            "summary = COALESCE(EXCLUDED.summary, rulings.summary)",
        ]
        for fragment in expected_correctable_fragments:
            assert fragment in sql, (
                f"Expected incoming-wins COALESCE for correctable field — "
                f"missing fragment {fragment!r} in SQL:\n{sql}"
            )


class TestInsertRulingForceUpdateIdentityAnchors:
    """Regression tests for #2475 — ``force_update=True`` in ``insert_ruling`` still
    overwrites identity anchors ``case_id`` and ``judge_id`` with ``EXCLUDED.*``.

    This preserves the reingest path (#2405 / #2431) where
    ``scripts/reingest_from_s3.py`` deliberately re-routes rulings to the
    correct case or judge after an extraction-logic fix.
    """

    def test_force_update_overwrites_case_id_unconditionally(self) -> None:
        conn = _mock_conn()
        insert_ruling(
            conn,
            document_id="doc-1",
            case_id="case-2-corrected",
            court_id="court-1",
            hearing_date=date(2026, 3, 5),
            ruling_text="Motion GRANTED",
            department="Dept. 1",
            judge_id="judge-1",
            force_update=True,
        )

        sql = _insert_ruling_conflict_sql(conn)
        assert "case_id = EXCLUDED.case_id" in sql, (
            f"Expected unconditional overwrite for case_id in force_update mode, got SQL:\n{sql}"
        )
        # No preserve-first COALESCE in force_update mode.
        assert "COALESCE(rulings.case_id" not in sql

    def test_force_update_overwrites_judge_id_unconditionally(self) -> None:
        conn = _mock_conn()
        insert_ruling(
            conn,
            document_id="doc-1",
            case_id="case-1",
            court_id="court-1",
            hearing_date=date(2026, 3, 5),
            ruling_text="Motion GRANTED",
            department="Dept. 1",
            judge_id="judge-2-corrected",
            force_update=True,
        )

        sql = _insert_ruling_conflict_sql(conn)
        assert "judge_id = EXCLUDED.judge_id" in sql, (
            f"Expected unconditional overwrite for judge_id in force_update mode, got SQL:\n{sql}"
        )
        assert "COALESCE(rulings.judge_id" not in sql


class TestInsertRulingContentHashSupersede:
    """Regression tests for #2458 — the content-hash fallback must supersede
    the losing document rather than running a stale-UPDATE-by-hash.

    Before #2458, a ``uq_rulings_case_text_hash`` UniqueViolation triggered
    ``UPDATE rulings SET ... WHERE case_id = %s AND ruling_text_hash = %s``.
    That WHERE clause matched the *winner*'s row (already present under a
    different document_id), so the winner's fields were silently overwritten
    with the loser's fields and the loser's document was left with stale
    ``ruling_text`` from before reingest — the exact bug reported in #2458.

    The fix supersedes the losing document (marks ``status = 'superseded'``
    and deletes any stale ruling keyed on its document_id).  The winner's
    ruling row is untouched: its text is identical by definition (the hashes
    matched), and its fields were populated by whichever document was
    processed first.
    """

    def test_supersede_sequence_deletes_then_marks_status(self) -> None:
        """DELETE FROM rulings runs before UPDATE documents so the losing
        document leaves no orphan ruling row behind."""
        import psycopg.errors

        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        exc = psycopg.errors.UniqueViolation("duplicate key value violates unique constraint")

        def side_effect_execute(sql: str, params: tuple | None = None) -> None:
            if "INSERT INTO rulings" in sql:
                raise exc

        cur.execute = MagicMock(side_effect=side_effect_execute)

        insert_ruling(
            conn,
            document_id="losing-doc",
            case_id="case-1",
            court_id="court-1",
            hearing_date=date(2026, 3, 5),
            ruling_text="Motion GRANTED",
            department="Dept. 1",
        )

        execute_calls = cur.execute.call_args_list
        # Locate the DELETE and the UPDATE documents call indices.
        delete_idx = next(
            i
            for i, c in enumerate(execute_calls)
            if "DELETE FROM rulings WHERE document_id" in c[0][0]
        )
        update_idx = next(
            i
            for i, c in enumerate(execute_calls)
            if "UPDATE documents SET status = 'superseded'" in c[0][0]
        )
        assert delete_idx < update_idx, (
            "DELETE FROM rulings must run before UPDATE documents so the "
            "superseded document leaves no orphaned ruling row."
        )

    def test_supersede_targets_losing_not_winning_document_id(self) -> None:
        """The supersede DELETE/UPDATE must target the losing document_id
        (the one currently being inserted), NOT the winner's row.  This is
        the core #2458 regression — the old fallback UPDATE used the
        winner's WHERE clause and mutated the wrong row.
        """
        import psycopg.errors

        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        exc = psycopg.errors.UniqueViolation("duplicate key value violates unique constraint")

        def side_effect_execute(sql: str, params: tuple | None = None) -> None:
            if "INSERT INTO rulings" in sql:
                raise exc

        cur.execute = MagicMock(side_effect=side_effect_execute)

        insert_ruling(
            conn,
            document_id="losing-doc-id",
            case_id="case-1",
            court_id="court-1",
            hearing_date=date(2026, 3, 5),
            ruling_text="Motion GRANTED",
            department="Dept. 1",
        )

        execute_calls = cur.execute.call_args_list
        delete_calls = [c for c in execute_calls if "DELETE FROM rulings" in c[0][0]]
        assert delete_calls
        assert delete_calls[0][0][1] == ("losing-doc-id",)

        doc_update_calls = [
            c for c in execute_calls if "UPDATE documents" in c[0][0] and "superseded" in c[0][0]
        ]
        assert doc_update_calls
        # The loser's document_id is always the LAST positional param
        # (after #2569, the UPDATE also carries the winner_id in earlier
        # positions when a winner is resolved).
        update_params = doc_update_calls[0][0][1]
        assert update_params[-1] == "losing-doc-id"

    def test_supersede_does_not_run_legacy_fallback_update(self) -> None:
        """The legacy ``UPDATE rulings SET ... WHERE case_id = ? AND
        ruling_text_hash = ?`` fallback must not run — that path was the
        #2458 bug."""
        import psycopg.errors

        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        exc = psycopg.errors.UniqueViolation("duplicate key value violates unique constraint")

        def side_effect_execute(sql: str, params: tuple | None = None) -> None:
            if "INSERT INTO rulings" in sql:
                raise exc

        cur.execute = MagicMock(side_effect=side_effect_execute)

        insert_ruling(
            conn,
            document_id="losing-doc",
            case_id="case-1",
            court_id="court-1",
            hearing_date=date(2026, 3, 5),
            ruling_text="Motion GRANTED",
            department="Dept. 1",
            judge_id="judge-different",
            outcome="denied",
        )

        sql_stmts = [call[0][0] for call in cur.execute.call_args_list]
        assert not any("UPDATE rulings SET" in s and "ruling_text_hash" in s for s in sql_stmts), (
            "Content-hash-based UPDATE rulings must not run on supersede — "
            "it was the #2458 bug that mutated the winner's row with the "
            "loser's fields."
        )
        # And the loser's (judge_id / outcome / department / etc.) fields
        # must not appear in any UPDATE rulings statement, because the
        # winner's row must not be touched.
        update_rulings_calls = [
            c for c in cur.execute.call_args_list if "UPDATE rulings SET" in c[0][0]
        ]
        assert update_rulings_calls == [], "No UPDATE rulings SET must run in the supersede path."


class TestInsertRulingReRouteScenario:
    """Scenario test mirroring the #2468 Csicsery-vs-Vincent scenario, at the
    ruling level.

    Setup: a document already ingested against ``case_id = case-csicsery``.
    The document is re-ingested (same document_id) and an upstream mis-routing
    bug re-routes the ruling to ``case_id = case-vincent`` and ``judge_id =
    judge-wrong``.  In default (live-ingestion) mode, the ON CONFLICT clause
    must preserve the originally-linked case and judge — the fix is the SQL
    literal, not a runtime check, so the test asserts on the generated SQL.
    """

    def test_live_ingestion_reroute_is_silently_ignored(self) -> None:
        conn = _mock_conn()
        insert_ruling(
            conn,
            document_id="doc-csicsery-ruling",
            case_id="case-vincent-wrong",  # silent re-route attempt
            court_id="court-contra-costa",
            hearing_date=date(2026, 3, 5),
            ruling_text="The motion is GRANTED.",
            department="Probate",
            judge_id="judge-wrong",
        )

        sql = _insert_ruling_conflict_sql(conn)
        # Preserve-first COALESCE must guard both identity anchors.
        assert "case_id = COALESCE(rulings.case_id, EXCLUDED.case_id)" in sql
        assert "judge_id = COALESCE(rulings.judge_id, EXCLUDED.judge_id)" in sql


class TestInsertDocumentPreservesCaseId:
    """Regression tests for #2475 — ``insert_document`` must not silently re-route
    a document to a different case on ON CONFLICT.

    The pre-#2475 SQL had a raw ``case_id = EXCLUDED.case_id`` — strictly worse
    than COALESCE because it would even overwrite a good existing case_id with
    NULL (though ``case_id`` is NOT NULL so NULL is impossible in practice, the
    overwrite-on-non-null was still a silent re-route).  Default mode now uses
    preserve-first ``COALESCE(documents.case_id, EXCLUDED.case_id)``.
    """

    def test_insert_document_default_preserves_case_id(self) -> None:
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = (True,)

        insert_document(
            conn,
            document_id="doc-1",
            case_id="case-1",
            court_id="court-1",
            content_format="html",
            content_hash="abc123",
            s3_key="rulings/doc-1.html",
            s3_bucket="judgemind-docs",
            source_url="https://example.com/ruling.html",
            scraper_id="scraper-oc",
            captured_at=datetime(2026, 3, 5, 10, 0, 0),
            hearing_date=date(2026, 3, 10),
        )

        sql = _insert_document_sql(conn)
        assert "case_id = COALESCE(documents.case_id, EXCLUDED.case_id)" in sql, (
            f"Expected preserve-first COALESCE for case_id in insert_document, got SQL:\n{sql}"
        )
        # The old unconditional-overwrite form must not appear.
        # Grep specifically for the ON CONFLICT SET clause pattern,
        # not the INSERT column list (which does contain "case_id" alone).
        assert "case_id = EXCLUDED.case_id" not in sql, (
            f"Legacy unconditional overwrite for case_id found in "
            f"insert_document — this is the #2475 identity-anchor bug. "
            f"SQL:\n{sql}"
        )
        # Guard against a regression to the original buggy COALESCE order.
        assert "COALESCE(EXCLUDED.case_id" not in sql

    def test_insert_document_force_update_overwrites_case_id(self) -> None:
        """``force_update=True`` threads through to the ON CONFLICT clause and
        restores unconditional overwrite — the reingest correction path."""
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = (True,)

        insert_document(
            conn,
            document_id="doc-1",
            case_id="case-2-corrected",
            court_id="court-1",
            content_format="html",
            content_hash="abc123",
            s3_key="rulings/doc-1.html",
            s3_bucket="judgemind-docs",
            source_url="https://example.com/ruling.html",
            scraper_id="scraper-oc",
            captured_at=datetime(2026, 3, 5, 10, 0, 0),
            hearing_date=date(2026, 3, 10),
            force_update=True,
        )

        sql = _insert_document_sql(conn)
        assert "case_id = EXCLUDED.case_id" in sql, (
            f"Expected unconditional overwrite for case_id in force_update mode, got SQL:\n{sql}"
        )
        assert "COALESCE(documents.case_id" not in sql

    def test_insert_document_hearing_date_still_correctable_default(self) -> None:
        """``hearing_date`` remains a correctable fact — default mode uses
        incoming-wins COALESCE(EXCLUDED.hearing_date, documents.hearing_date).
        """
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = (True,)

        insert_document(
            conn,
            document_id="doc-1",
            case_id="case-1",
            court_id="court-1",
            content_format="html",
            content_hash="abc123",
            s3_key=None,
            s3_bucket=None,
            source_url="https://example.com",
            scraper_id="scraper-1",
            captured_at=datetime(2026, 3, 5),
            hearing_date=date(2026, 3, 10),
        )

        sql = _insert_document_sql(conn)
        assert "hearing_date = COALESCE(EXCLUDED.hearing_date, documents.hearing_date)" in sql
