"""Tests for backfill_ruling_html script.

Tests cover:
- count_pending with and without county filter
- fetch_batch with cursor-based pagination
- format_one_ruling success and error handling
- process_batch with concurrency, dry-run, and error recovery
- run_backfill end-to-end with mocked DB and LLM
- CLI argument parsing
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

# Add scripts directory to path so we can import the module
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "packages", "scraper-framework", "src")
)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ["_VENV_HELPER_SKIP"] = "1"

from backfill_ruling_html import (
    _CURSOR_MIN_TIMESTAMP,
    _CURSOR_MIN_UUID,
    count_pending,
    fetch_batch,
    format_one_ruling,
    process_batch,
    run_backfill,
)


# ---------------------------------------------------------------------------
# count_pending
# ---------------------------------------------------------------------------


def test_count_pending_no_county() -> None:
    """Counts pending rulings without county filter."""
    conn = MagicMock()
    cursor_mock = MagicMock()
    cursor_mock.fetchone.return_value = (42,)
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor_mock)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    result = count_pending(conn)
    assert result == 42
    # Should use the query without county
    executed_sql = cursor_mock.execute.call_args[0][0]
    assert "county" not in executed_sql.lower()


def test_count_pending_with_county() -> None:
    """Counts pending rulings filtered by county."""
    conn = MagicMock()
    cursor_mock = MagicMock()
    cursor_mock.fetchone.return_value = (10,)
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor_mock)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    result = count_pending(conn, county="Los Angeles")
    assert result == 10
    # Should use the query with county
    executed_sql = cursor_mock.execute.call_args[0][0]
    assert "county" in executed_sql.lower()
    assert cursor_mock.execute.call_args[0][1] == ("Los Angeles",)


# ---------------------------------------------------------------------------
# fetch_batch
# ---------------------------------------------------------------------------


def test_fetch_batch_no_county() -> None:
    """Fetches a batch of rulings without county filter."""
    conn = MagicMock()
    cursor_mock = MagicMock()
    ts = datetime(2025, 1, 1)
    cursor_mock.fetchall.return_value = [
        ("uuid-1", "ruling text 1", ts),
        ("uuid-2", "ruling text 2", ts),
    ]
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor_mock)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    cursor = (_CURSOR_MIN_TIMESTAMP, _CURSOR_MIN_UUID)
    rows = fetch_batch(conn, 10, cursor)
    assert len(rows) == 2
    assert rows[0] == ("uuid-1", "ruling text 1", ts)


def test_fetch_batch_with_county() -> None:
    """Fetches a batch filtered by county."""
    conn = MagicMock()
    cursor_mock = MagicMock()
    cursor_mock.fetchall.return_value = []
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor_mock)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    cursor = (_CURSOR_MIN_TIMESTAMP, _CURSOR_MIN_UUID)
    rows = fetch_batch(conn, 10, cursor, county="Orange")
    assert len(rows) == 0
    executed_sql = cursor_mock.execute.call_args[0][0]
    assert "county" in executed_sql.lower()


# ---------------------------------------------------------------------------
# format_one_ruling
# ---------------------------------------------------------------------------


@patch("backfill_ruling_html.format_ruling_text")
def test_format_one_ruling_success(mock_fmt: MagicMock) -> None:
    """Successful formatting returns HTML."""
    mock_fmt.return_value = '<div class="ruling"><p>Formatted</p></div>'
    rid, html, err = format_one_ruling("uuid-1", "Some ruling text")
    assert rid == "uuid-1"
    assert html == '<div class="ruling"><p>Formatted</p></div>'
    assert err is None


@patch("backfill_ruling_html.format_ruling_text")
def test_format_one_ruling_exception(mock_fmt: MagicMock) -> None:
    """LLM exception is caught and returned as error string."""
    mock_fmt.side_effect = RuntimeError("API down")
    rid, html, err = format_one_ruling("uuid-1", "Some ruling text")
    assert rid == "uuid-1"
    assert html is None
    assert "API down" in err


# ---------------------------------------------------------------------------
# process_batch
# ---------------------------------------------------------------------------


@patch("backfill_ruling_html.format_ruling_text")
def test_process_batch_formats_and_updates(mock_fmt: MagicMock) -> None:
    """Formats rulings and writes to DB."""
    mock_fmt.return_value = "<p>Formatted</p>"

    conn = MagicMock()
    cursor_mock = MagicMock()
    cursor_mock.rowcount = 1
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor_mock)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    ts = datetime(2025, 1, 1)
    rows = [
        ("uuid-1", "ruling text 1", ts),
        ("uuid-2", "ruling text 2", ts),
    ]

    formatted, skipped, errors = process_batch(conn, rows, concurrency=1)
    assert formatted == 2
    assert skipped == 0
    assert errors == 0
    conn.commit.assert_called_once()


@patch("backfill_ruling_html.format_ruling_text")
def test_process_batch_dry_run(mock_fmt: MagicMock) -> None:
    """Dry run does not write to DB."""
    mock_fmt.return_value = "<p>Formatted</p>"

    conn = MagicMock()
    ts = datetime(2025, 1, 1)
    rows = [("uuid-1", "ruling text 1", ts)]

    formatted, skipped, errors = process_batch(
        conn, rows, concurrency=1, dry_run=True
    )
    assert formatted == 1
    assert errors == 0
    conn.rollback.assert_called_once()
    conn.commit.assert_not_called()


@patch("backfill_ruling_html.format_ruling_text")
def test_process_batch_handles_errors(mock_fmt: MagicMock) -> None:
    """Errors on individual rulings are logged, not raised."""
    mock_fmt.side_effect = RuntimeError("LLM error")

    conn = MagicMock()
    ts = datetime(2025, 1, 1)
    rows = [("uuid-1", "ruling text 1", ts)]

    formatted, skipped, errors = process_batch(conn, rows, concurrency=1)
    assert formatted == 0
    assert errors == 1
    # Should still commit (error rulings are skipped, not fatal)
    conn.commit.assert_called_once()


@patch("backfill_ruling_html.format_ruling_text")
def test_process_batch_skips_already_formatted(mock_fmt: MagicMock) -> None:
    """If UPDATE rowcount is 0 (concurrent run), counts as skipped."""
    mock_fmt.return_value = "<p>Formatted</p>"

    conn = MagicMock()
    cursor_mock = MagicMock()
    cursor_mock.rowcount = 0  # Already formatted by another run
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor_mock)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    ts = datetime(2025, 1, 1)
    rows = [("uuid-1", "ruling text 1", ts)]

    formatted, skipped, errors = process_batch(conn, rows, concurrency=1)
    assert formatted == 0
    assert skipped == 1


# ---------------------------------------------------------------------------
# run_backfill
# ---------------------------------------------------------------------------


@patch("backfill_ruling_html.create_client")
@patch("backfill_ruling_html.format_ruling_text")
@patch("backfill_ruling_html.psycopg")
def test_run_backfill_processes_all(
    mock_psycopg: MagicMock,
    mock_fmt: MagicMock,
    mock_create_client: MagicMock,
) -> None:
    """End-to-end backfill processes and commits."""
    mock_create_client.return_value = MagicMock()
    mock_fmt.return_value = "<p>Formatted</p>"

    ts = datetime(2025, 1, 1)
    conn = MagicMock()
    mock_psycopg.connect.return_value.__enter__ = MagicMock(return_value=conn)
    mock_psycopg.connect.return_value.__exit__ = MagicMock(return_value=False)

    # Mock cursor for count_pending -> returns 2
    count_cursor = MagicMock()
    count_cursor.fetchone.return_value = (2,)

    # Mock cursor for fetch_batch -> returns 2 rows, then empty
    fetch_cursor = MagicMock()
    fetch_cursor.fetchall.side_effect = [
        [("uuid-1", "text 1", ts), ("uuid-2", "text 2", ts)],
        [],  # second call returns empty
    ]

    # Mock cursor for UPDATE
    update_cursor = MagicMock()
    update_cursor.rowcount = 1

    # Sequence: count query, then fetch, then updates, then fetch again
    conn.cursor.return_value.__enter__ = MagicMock(
        side_effect=[count_cursor, fetch_cursor, update_cursor, update_cursor, fetch_cursor]
    )
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    stats = run_backfill("postgresql://test", batch_size=10, concurrency=1)

    assert stats["total_processed"] == 2
    assert stats["total_formatted"] == 2
    assert stats["total_errors"] == 0


@patch("backfill_ruling_html.create_client")
@patch("backfill_ruling_html.format_ruling_text")
@patch("backfill_ruling_html.psycopg")
def test_run_backfill_with_limit(
    mock_psycopg: MagicMock,
    mock_fmt: MagicMock,
    mock_create_client: MagicMock,
) -> None:
    """Limit flag restricts number of rulings processed."""
    mock_create_client.return_value = MagicMock()
    mock_fmt.return_value = "<p>Formatted</p>"

    ts = datetime(2025, 1, 1)
    conn = MagicMock()
    mock_psycopg.connect.return_value.__enter__ = MagicMock(return_value=conn)
    mock_psycopg.connect.return_value.__exit__ = MagicMock(return_value=False)

    count_cursor = MagicMock()
    count_cursor.fetchone.return_value = (100,)

    fetch_cursor = MagicMock()
    fetch_cursor.fetchall.return_value = [("uuid-1", "text 1", ts)]

    update_cursor = MagicMock()
    update_cursor.rowcount = 1

    conn.cursor.return_value.__enter__ = MagicMock(
        side_effect=[count_cursor, fetch_cursor, update_cursor]
    )
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    stats = run_backfill(
        "postgresql://test", batch_size=10, concurrency=1, limit=1
    )

    assert stats["total_processed"] == 1


@patch("backfill_ruling_html.create_client")
@patch("backfill_ruling_html.format_ruling_text")
@patch("backfill_ruling_html.psycopg")
def test_run_backfill_dry_run(
    mock_psycopg: MagicMock,
    mock_fmt: MagicMock,
    mock_create_client: MagicMock,
) -> None:
    """Dry run does not commit."""
    mock_create_client.return_value = MagicMock()
    mock_fmt.return_value = "<p>Formatted</p>"

    ts = datetime(2025, 1, 1)
    conn = MagicMock()
    mock_psycopg.connect.return_value.__enter__ = MagicMock(return_value=conn)
    mock_psycopg.connect.return_value.__exit__ = MagicMock(return_value=False)

    count_cursor = MagicMock()
    count_cursor.fetchone.return_value = (1,)

    fetch_cursor = MagicMock()
    fetch_cursor.fetchall.side_effect = [
        [("uuid-1", "text 1", ts)],
        [],
    ]

    conn.cursor.return_value.__enter__ = MagicMock(
        side_effect=[count_cursor, fetch_cursor, fetch_cursor]
    )
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    stats = run_backfill(
        "postgresql://test", batch_size=10, concurrency=1, dry_run=True
    )

    assert stats["total_formatted"] == 1
    conn.commit.assert_not_called()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_main_missing_database_url() -> None:
    """Main exits with error if DATABASE_URL is not set."""
    with patch.dict(os.environ, {}, clear=True):
        # Remove DATABASE_URL if present
        os.environ.pop("DATABASE_URL", None)
        os.environ["_VENV_HELPER_SKIP"] = "1"
        from backfill_ruling_html import main

        with pytest.raises(SystemExit) as exc_info:
            with patch("sys.argv", ["backfill_ruling_html.py"]):
                main()
        assert exc_info.value.code == 1
