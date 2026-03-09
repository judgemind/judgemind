"""Tests for NUL byte stripping in ingestion/db.py.

Verifies that all text fields passed to PostgreSQL have NUL (0x00) bytes
removed at the DB layer, protecting all callers from Postgres text field errors.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

from ingestion.db import (
    _strip_nul,
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
        for call in cur.execute.call_args_list:
            call_args = call[0][1]
            for arg in call_args:
                if isinstance(arg, str):
                    assert "\x00" not in arg, f"NUL byte found in arg: {arg!r}"
