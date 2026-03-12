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
    _looks_like_valid_judge_name,
    _strip_nul,
    _truncate_party_name,
    batch_upsert_parties,
    insert_document,
    insert_ruling,
    normalize_judge_name,
    normalize_party_name,
    resolve_judge,
    upsert_case,
    upsert_case_judge,
    upsert_case_party,
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
    """Extract the parameter tuple from the last cursor.execute() call."""
    cur = conn.cursor.return_value.__enter__.return_value
    return cur.execute.call_args[0][1]


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
        # The SELECT returns both parties as already existing
        cur.fetchall.return_value = [
            ("John Doe", "party-1"),
            ("Jane Smith", "party-2"),
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
        # SELECT returns one existing alias
        cur.fetchall.side_effect = [
            [("John Doe", "existing-pid")],  # batch alias lookup
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

    def test_long_party_name_truncated(self) -> None:
        """Party names exceeding _MAX_PARTY_NAME_LENGTH are truncated."""
        conn, cur = _mock_conn_for_batch()
        cur.fetchall.side_effect = [[]]
        cur.fetchone.side_effect = [("pid-1",)]
        cur.nextset.side_effect = [False]

        long_name = "A" * 9000
        batch_upsert_parties(
            conn,
            "case-1",
            [{"name": long_name, "role": "plaintiff"}],
        )

        # The SELECT should use the truncated name
        select_args = cur.execute.call_args_list[0][0][1]
        raw_names_list = select_args[0]
        assert len(raw_names_list[0]) == _MAX_PARTY_NAME_LENGTH


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

    def test_long_name_truncated_before_insert(self) -> None:
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        # First fetchone: no existing alias; second: new party id
        cur.fetchone.side_effect = [None, ("party-uuid-1",)]

        long_name = "C" * 9000
        upsert_party(conn, raw_name=long_name, party_type="plaintiff")

        # All execute calls should use truncated names
        for c in cur.execute.call_args_list:
            call_args = c[0][1]
            for arg in call_args:
                if isinstance(arg, str):
                    assert len(arg) <= _MAX_PARTY_NAME_LENGTH, (
                        f"Arg length {len(arg)} exceeds limit"
                    )


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
        # First fetchone: no existing alias; second: new judge id
        cur.fetchone.side_effect = [None, ("new-judge-id",)]
        result = resolve_judge(conn, "Hon. John Smith", "court-1")
        assert result == "new-judge-id"
        # Should have 3 execute calls: SELECT alias, INSERT judge, INSERT alias
        assert cur.execute.call_count == 3

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
        # First fetchone: no alias; second: INSERT returns None
        cur.fetchone.side_effect = [None, None]
        with pytest.raises(RuntimeError, match="resolve_judge"):
            resolve_judge(conn, "John Smith", "court-1")


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


# ---------------------------------------------------------------------------
# upsert_case_party
# ---------------------------------------------------------------------------


class TestUpsertCaseParty:
    """Tests for upsert_case_party function."""

    def test_passes_correct_params(self) -> None:
        conn = _mock_conn()
        upsert_case_party(conn, "case-1", "party-1", "plaintiff")
        args = _get_execute_args(conn)
        # The function passes (case_id, party_id, role) twice for the WHERE NOT EXISTS
        assert args == ("case-1", "party-1", "plaintiff", "case-1", "party-1", "plaintiff")

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
