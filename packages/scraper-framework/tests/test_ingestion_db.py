"""Tests for ingestion/db.py.

Covers:
  - NUL byte stripping in text fields
  - batch_upsert_parties function
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

from ingestion.db import (
    _strip_nul,
    batch_upsert_parties,
    insert_ruling,
    upsert_case,
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
