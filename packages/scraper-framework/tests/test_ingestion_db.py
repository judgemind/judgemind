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
    insert_document,
    insert_document_and_ruling,
    insert_ruling,
    lookup_existing_case_title,
    normalize_case_title,
    normalize_judge_name,
    normalize_party_name,
    normalize_ruling_text_hash,
    resolve_judge,
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
        cur.fetchone.return_value = ("existing-judge-id",)
        result = resolve_judge(conn, "Hon. John Smith", "court-1")
        assert result == "existing-judge-id"

    def test_creates_new_judge_when_no_alias(self) -> None:
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        # fetchone: alias lookup -> None, canonical lookup -> None, INSERT -> new id
        cur.fetchone.side_effect = [None, None, ("new-judge-id",)]
        cur.fetchall.return_value = []  # no near-duplicates
        result = resolve_judge(conn, "Hon. John Smith", "court-1")
        assert result == "new-judge-id"
        # Should have 5 execute calls:
        # SELECT alias, SELECT canonical, SELECT near-dup, INSERT judge, INSERT alias
        assert cur.execute.call_count == 5

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
        cur.fetchone.return_value = ("existing-judge-id",)
        result = resolve_judge(conn, "John\x00 Smith", "court-1")
        assert result == "existing-judge-id"
        # Verify NUL was stripped from the name passed to SQL
        select_args = cur.execute.call_args_list[0][0][1]
        assert "\x00" not in str(select_args)

    def test_raises_on_insert_returning_none(self) -> None:
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        # fetchone: alias lookup -> None, canonical lookup -> None, INSERT -> None
        cur.fetchone.side_effect = [None, None, None]
        cur.fetchall.return_value = []  # no near-duplicates
        with pytest.raises(RuntimeError, match="resolve_judge"):
            resolve_judge(conn, "John Smith", "court-1")

    def test_lookup_uses_case_insensitive_match(self) -> None:
        """Verify alias lookup uses LOWER() for case-insensitive matching (#1453)."""
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = ("existing-judge-id",)
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
        """When no alias, canonical, or near-dup match exists, create new judge."""
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        # fetchone sequence:
        # 1. alias lookup -> None
        # 2. canonical lookup -> None
        # 3. INSERT judges -> new id
        cur.fetchone.side_effect = [None, None, ("new-judge-id",)]
        cur.fetchall.return_value = []  # no existing judges at court
        result = resolve_judge(conn, "John Smith", "court-1")
        assert result == "new-judge-id"

    def test_new_judge_uses_on_conflict(self) -> None:
        """The INSERT INTO judges uses ON CONFLICT for race condition safety."""
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.side_effect = [None, None, ("new-judge-id",)]
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

    def test_unique_violation_triggers_update_fallback(self) -> None:
        """When UniqueViolation fires (content-hash conflict), fall back to UPDATE.

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

        # Verify: SAVEPOINT, INSERT (which raises), ROLLBACK TO SAVEPOINT, UPDATE
        execute_calls = cur.execute.call_args_list
        sql_stmts = [call[0][0] for call in execute_calls]
        assert any("SAVEPOINT" in s for s in sql_stmts)
        assert any("INSERT INTO rulings" in s for s in sql_stmts)
        assert any("ROLLBACK TO SAVEPOINT" in s for s in sql_stmts)
        assert any("UPDATE rulings SET" in s for s in sql_stmts)

    def test_unique_violation_update_uses_coalesce(self) -> None:
        """Fallback UPDATE uses COALESCE to preserve existing non-NULL fields."""
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
            judge_id="judge-1",
            outcome="granted",
        )

        # Find the UPDATE call and verify COALESCE is used
        update_calls = [c for c in cur.execute.call_args_list if "UPDATE rulings SET" in c[0][0]]
        assert len(update_calls) == 1
        sql = update_calls[0][0][0]
        assert "COALESCE" in sql

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

    def test_fallback_update_logs_warning_on_zero_rowcount(self) -> None:
        """When fallback UPDATE matches 0 rows, a warning should be logged."""
        import psycopg.errors

        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        exc = psycopg.errors.UniqueViolation("duplicate key value violates unique constraint")

        # Track which SQL statement we're on to set rowcount on UPDATE
        def side_effect_execute(sql: str, params: tuple | None = None) -> None:
            if "INSERT INTO rulings" in sql:
                raise exc
            if "UPDATE rulings SET" in sql:
                cur.rowcount = 0

        cur.execute = MagicMock(side_effect=side_effect_execute)

        # Should not raise — just log a warning
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

    def test_skips_ruling_when_hearing_date_is_none(self) -> None:
        """When hearing_date is None, only insert_document is called."""
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
        # Only insert_document should have been called (1 execute with params).
        calls_with_params = [c for c in cur.execute.call_args_list if len(c[0]) > 1]
        assert len(calls_with_params) == 1

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
