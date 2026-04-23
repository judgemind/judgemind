"""Tests for the capture-path content-hash deduplication helper.

Covers:
- content_hash_seen_at: hit within window, outside window, miss, no conn, DB exception
- record_capture_dedup_skipped: happy path insert, exception swallowed
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# content_hash_seen_at
# ---------------------------------------------------------------------------


def test_hit_within_window_returns_winner_id() -> None:
    """When a row exists within the window, return the winner document id."""
    from framework.capture_dedup import content_hash_seen_at

    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__ = lambda s: mock_cur
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    winner_id = "abc123-winner-uuid"
    mock_cur.fetchone.return_value = (winner_id,)

    result = content_hash_seen_at(mock_conn, "Los Angeles", "deadbeef" * 8, days=14)

    assert result == winner_id
    mock_cur.execute.assert_called_once()
    # Check that county and hash were passed as params
    call_args = mock_cur.execute.call_args
    assert "Los Angeles" in call_args[0][1]
    assert "deadbeef" * 8 in call_args[0][1]


def test_miss_returns_none() -> None:
    """When no row matches, return None."""
    from framework.capture_dedup import content_hash_seen_at

    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__ = lambda s: mock_cur
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    mock_cur.fetchone.return_value = None

    result = content_hash_seen_at(mock_conn, "Orange", "cafebabe" * 8)

    assert result is None


def test_outside_window_returns_none() -> None:
    """When query returns no row (filtered by date window), return None."""
    from framework.capture_dedup import content_hash_seen_at

    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__ = lambda s: mock_cur
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    # fetchone returns None because the row is outside the window (DB filters it)
    mock_cur.fetchone.return_value = None

    result = content_hash_seen_at(mock_conn, "Orange", "cafebabe" * 8, days=1)

    assert result is None


def test_no_conn_returns_none() -> None:
    """When conn is None, return None without raising."""
    from framework.capture_dedup import content_hash_seen_at

    result = content_hash_seen_at(None, "Los Angeles", "deadbeef" * 8)

    assert result is None


def test_db_exception_returns_none() -> None:
    """When the DB raises, return None (best-effort, never blocks scraper)."""
    from framework.capture_dedup import content_hash_seen_at

    mock_conn = MagicMock()
    mock_conn.cursor.side_effect = Exception("connection lost")

    result = content_hash_seen_at(mock_conn, "Los Angeles", "deadbeef" * 8)

    assert result is None


# ---------------------------------------------------------------------------
# record_capture_dedup_skipped
# ---------------------------------------------------------------------------


def test_record_capture_dedup_skipped_inserts_row() -> None:
    """Happy path: inserts one row into telemetry.data_quality_metrics."""
    from framework.capture_dedup import record_capture_dedup_skipped

    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__ = lambda s: mock_cur
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    record_capture_dedup_skipped(
        mock_conn,
        county="Los Angeles",
        content_hash="deadbeef" * 8,
        winner_document_id="winner-uuid",
        new_source_url="https://example.com/ruling/2",
        scraper_id="ca-la-tentatives-civil",
    )

    mock_cur.execute.assert_called_once()
    sql, params = mock_cur.execute.call_args[0]
    assert "data_quality_metrics" in sql
    assert "capture_content_hash_dedup_skipped" in params
    # metadata JSON should contain expected keys
    metadata = json.loads(params[-1])
    assert metadata["winner_document_id"] == "winner-uuid"
    assert metadata["new_source_url"] == "https://example.com/ruling/2"
    assert metadata["scraper_id"] == "ca-la-tentatives-civil"


def test_record_capture_dedup_skipped_swallows_exceptions() -> None:
    """When the DB raises during telemetry insert, the exception is swallowed."""
    from framework.capture_dedup import record_capture_dedup_skipped

    mock_conn = MagicMock()
    mock_conn.cursor.side_effect = Exception("telemetry DB unavailable")

    # Must not raise
    record_capture_dedup_skipped(
        mock_conn,
        county="Los Angeles",
        content_hash="deadbeef" * 8,
        winner_document_id="winner-uuid",
        new_source_url="https://example.com/ruling/2",
        scraper_id="ca-la-tentatives-civil",
    )
