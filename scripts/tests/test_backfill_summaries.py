"""Tests for backfill_summaries script.

Tests cover:
- count_pending with and without county filter
- fetch_batch with cursor-based pagination
- summarize_one_ruling success and error handling
- generate_summary builds correct prompt
- process_batch with concurrency, dry-run, and error recovery
- run_backfill end-to-end with mocked DB and LLM
- CLI argument parsing

The script depends on psycopg and anthropic, which are not available in
the lightweight CI scripts-tests environment.  We mock them in
sys.modules before importing the script under test.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Pre-import mocking — the script imports psycopg and anthropic at module
# level, which are not installed in the scripts-tests CI environment.
# We inject mocks before importing.
# ---------------------------------------------------------------------------

# Add scripts directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Create mock modules for third-party dependencies
_mock_psycopg = MagicMock()
_mock_anthropic = MagicMock()

# judgemind_config provides DEFAULT_HAIKU_MODEL.  If the real package is not
# installed (lightweight CI environment), we provide a mock with the same
# constant so the script can be imported.
try:
    import judgemind_config as _real_jc  # noqa: F401

    _modules_to_mock_jc: dict[str, object] = {}
except ImportError:
    _mock_judgemind_config = MagicMock()
    _mock_judgemind_config.DEFAULT_HAIKU_MODEL = "claude-haiku-4-5-20251001"
    _modules_to_mock_jc = {"judgemind_config": _mock_judgemind_config}

_modules_to_mock = {
    "psycopg": _mock_psycopg,
    "anthropic": _mock_anthropic,
    **_modules_to_mock_jc,
}

# Inject mocks, remembering any that already exist so we restore them later.
_saved_modules: dict[str, object] = {}
for mod_name, mock_mod in _modules_to_mock.items():
    if mod_name in sys.modules:
        _saved_modules[mod_name] = sys.modules[mod_name]
    sys.modules[mod_name] = mock_mod

# Now import the script under test — it will pick up our mocks.
import backfill_summaries  # noqa: E402

# Re-bind the real functions/constants we need from the now-imported module.
_CURSOR_MIN_TIMESTAMP = backfill_summaries._CURSOR_MIN_TIMESTAMP
_CURSOR_MIN_UUID = backfill_summaries._CURSOR_MIN_UUID
count_pending = backfill_summaries.count_pending
fetch_batch = backfill_summaries.fetch_batch
summarize_one_ruling = backfill_summaries.summarize_one_ruling
generate_summary = backfill_summaries.generate_summary
process_batch = backfill_summaries.process_batch
run_backfill = backfill_summaries.run_backfill


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
    executed_sql = cursor_mock.execute.call_args[0][0]
    assert "county" in executed_sql.lower()
    assert cursor_mock.execute.call_args[0][1] == ("Los Angeles",)


def test_count_pending_empty_result() -> None:
    """Returns 0 if fetchone returns None."""
    conn = MagicMock()
    cursor_mock = MagicMock()
    cursor_mock.fetchone.return_value = None
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor_mock)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    result = count_pending(conn)
    assert result == 0


# ---------------------------------------------------------------------------
# fetch_batch
# ---------------------------------------------------------------------------


def test_fetch_batch_no_county() -> None:
    """Fetches a batch of rulings without county filter."""
    conn = MagicMock()
    cursor_mock = MagicMock()
    ts = datetime(2025, 1, 1)
    cursor_mock.fetchall.return_value = [
        ("uuid-1", "ruling text 1", ts, "msj", "24STCV00001", "Smith v. Jones"),
        ("uuid-2", "ruling text 2", ts, None, None, None),
    ]
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor_mock)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    cursor = (_CURSOR_MIN_TIMESTAMP, _CURSOR_MIN_UUID)
    rows = fetch_batch(conn, 10, cursor)
    assert len(rows) == 2
    assert rows[0] == (
        "uuid-1",
        "ruling text 1",
        ts,
        "msj",
        "24STCV00001",
        "Smith v. Jones",
    )
    assert rows[1] == ("uuid-2", "ruling text 2", ts, None, None, None)


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
# generate_summary
# ---------------------------------------------------------------------------


def test_generate_summary_basic() -> None:
    """Generates a summary using the Anthropic client."""
    mock_client = MagicMock()
    content_block = MagicMock()
    content_block.text = "  The court granted the motion.  "
    response = MagicMock()
    response.content = [content_block]
    mock_client.messages.create.return_value = response

    result = generate_summary("Some ruling text", mock_client)
    assert result == "The court granted the motion."

    # Verify correct model used — uses the centralized constant
    call_kwargs = mock_client.messages.create.call_args
    assert call_kwargs.kwargs["model"] == backfill_summaries._SUMMARY_MODEL


def test_generate_summary_with_context() -> None:
    """Case context is included in the user message."""
    mock_client = MagicMock()
    content_block = MagicMock()
    content_block.text = "A summary."
    response = MagicMock()
    response.content = [content_block]
    mock_client.messages.create.return_value = response

    result = generate_summary(
        "Ruling text",
        mock_client,
        case_context={"case_title": "Smith v. Jones", "motion_type": "MSJ"},
    )
    assert result == "A summary."

    call_kwargs = mock_client.messages.create.call_args
    user_content = call_kwargs.kwargs["messages"][0]["content"]
    assert "case_title: Smith v. Jones" in user_content
    assert "motion_type: MSJ" in user_content


def test_generate_summary_no_context() -> None:
    """Summary works without case context."""
    mock_client = MagicMock()
    content_block = MagicMock()
    content_block.text = "A summary."
    response = MagicMock()
    response.content = [content_block]
    mock_client.messages.create.return_value = response

    result = generate_summary("Ruling text", mock_client, case_context=None)
    assert result == "A summary."

    call_kwargs = mock_client.messages.create.call_args
    user_content = call_kwargs.kwargs["messages"][0]["content"]
    assert "Case context" not in user_content


def test_generate_summary_system_prompt_plain_language() -> None:
    """System prompt instructs for plain language output."""
    mock_client = MagicMock()
    content_block = MagicMock()
    content_block.text = "A summary."
    response = MagicMock()
    response.content = [content_block]
    mock_client.messages.create.return_value = response

    generate_summary("Ruling text", mock_client)

    call_kwargs = mock_client.messages.create.call_args
    system = call_kwargs.kwargs["system"]
    assert "plain language" in system.lower()
    assert "2-4 sentences" in system


# ---------------------------------------------------------------------------
# summarize_one_ruling
# ---------------------------------------------------------------------------


def test_summarize_one_ruling_success() -> None:
    """Successful summarization returns summary text."""
    mock_client = MagicMock()
    content_block = MagicMock()
    content_block.text = "The court denied the motion."
    response = MagicMock()
    response.content = [content_block]
    mock_client.messages.create.return_value = response

    rid, summary, err = summarize_one_ruling("uuid-1", "Some ruling text", mock_client)
    assert rid == "uuid-1"
    assert summary == "The court denied the motion."
    assert err is None


def test_summarize_one_ruling_exception() -> None:
    """LLM exception is caught and returned as error string."""
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = RuntimeError("API down")

    rid, summary, err = summarize_one_ruling("uuid-1", "Some ruling text", mock_client)
    assert rid == "uuid-1"
    assert summary is None
    assert "API down" in err


def test_summarize_one_ruling_with_context() -> None:
    """Case context is passed through to generate_summary."""
    mock_client = MagicMock()
    content_block = MagicMock()
    content_block.text = "A summary with context."
    response = MagicMock()
    response.content = [content_block]
    mock_client.messages.create.return_value = response

    ctx = {"case_title": "Smith v. Jones"}
    rid, summary, err = summarize_one_ruling(
        "uuid-1", "Ruling text", mock_client, case_context=ctx
    )
    assert summary == "A summary with context."

    call_kwargs = mock_client.messages.create.call_args
    user_content = call_kwargs.kwargs["messages"][0]["content"]
    assert "case_title: Smith v. Jones" in user_content


# ---------------------------------------------------------------------------
# process_batch
# ---------------------------------------------------------------------------


def test_process_batch_summarizes_and_updates() -> None:
    """Summarizes rulings and writes to DB."""
    mock_client = MagicMock()
    content_block = MagicMock()
    content_block.text = "A summary."
    response = MagicMock()
    response.content = [content_block]
    mock_client.messages.create.return_value = response

    conn = MagicMock()
    cursor_mock = MagicMock()
    cursor_mock.rowcount = 1
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor_mock)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    ts = datetime(2025, 1, 1)
    rows = [
        ("uuid-1", "ruling text 1", ts, "msj", "24STCV00001", "Smith v. Jones"),
        ("uuid-2", "ruling text 2", ts, None, None, None),
    ]

    summarized, skipped, errors = process_batch(
        conn, rows, client=mock_client, concurrency=1
    )
    assert summarized == 2
    assert skipped == 0
    assert errors == 0
    conn.commit.assert_called_once()


def test_process_batch_dry_run() -> None:
    """Dry run does not write to DB."""
    mock_client = MagicMock()
    content_block = MagicMock()
    content_block.text = "A summary."
    response = MagicMock()
    response.content = [content_block]
    mock_client.messages.create.return_value = response

    conn = MagicMock()
    ts = datetime(2025, 1, 1)
    rows = [("uuid-1", "ruling text 1", ts, None, None, None)]

    summarized, skipped, errors = process_batch(
        conn, rows, client=mock_client, concurrency=1, dry_run=True
    )
    assert summarized == 1
    assert errors == 0
    conn.rollback.assert_called_once()
    conn.commit.assert_not_called()


def test_process_batch_handles_errors() -> None:
    """Errors on individual rulings are logged, not raised."""
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = RuntimeError("LLM error")

    conn = MagicMock()
    ts = datetime(2025, 1, 1)
    rows = [("uuid-1", "ruling text 1", ts, None, None, None)]

    summarized, skipped, errors = process_batch(
        conn, rows, client=mock_client, concurrency=1
    )
    assert summarized == 0
    assert errors == 1
    # Should still commit (error rulings are skipped, not fatal)
    conn.commit.assert_called_once()


def test_process_batch_skips_already_summarized() -> None:
    """If UPDATE rowcount is 0 (concurrent run), counts as skipped."""
    mock_client = MagicMock()
    content_block = MagicMock()
    content_block.text = "A summary."
    response = MagicMock()
    response.content = [content_block]
    mock_client.messages.create.return_value = response

    conn = MagicMock()
    cursor_mock = MagicMock()
    cursor_mock.rowcount = 0  # Already summarized by another run
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor_mock)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    ts = datetime(2025, 1, 1)
    rows = [("uuid-1", "ruling text 1", ts, None, None, None)]

    summarized, skipped, errors = process_batch(
        conn, rows, client=mock_client, concurrency=1
    )
    assert summarized == 0
    assert skipped == 1


def test_process_batch_includes_case_context() -> None:
    """Verifies that case context (title, motion_type) is passed to the summarizer."""
    mock_client = MagicMock()
    content_block = MagicMock()
    content_block.text = "A summary with context."
    response = MagicMock()
    response.content = [content_block]
    mock_client.messages.create.return_value = response

    conn = MagicMock()
    cursor_mock = MagicMock()
    cursor_mock.rowcount = 1
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor_mock)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    ts = datetime(2025, 1, 1)
    rows = [("uuid-1", "ruling text 1", ts, "msj", "24STCV00001", "Smith v. Jones")]

    process_batch(conn, rows, client=mock_client, concurrency=1)

    # Verify the API was called with case context in the prompt
    call_kwargs = mock_client.messages.create.call_args
    user_content = call_kwargs.kwargs["messages"][0]["content"]
    assert "case_title: Smith v. Jones" in user_content
    assert "motion_type: msj" in user_content


# ---------------------------------------------------------------------------
# run_backfill
# ---------------------------------------------------------------------------


@patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"})
@patch("backfill_summaries.anthropic")
@patch("backfill_summaries.psycopg")
def test_run_backfill_processes_all(
    mock_psycopg: MagicMock,
    mock_anthropic: MagicMock,
) -> None:
    """End-to-end backfill processes and commits."""
    # Set up the mock Anthropic client
    mock_client = MagicMock()
    mock_anthropic.Anthropic.return_value = mock_client
    content_block = MagicMock()
    content_block.text = "A summary."
    response = MagicMock()
    response.content = [content_block]
    mock_client.messages.create.return_value = response

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
        [
            ("uuid-1", "text 1", ts, None, None, None),
            ("uuid-2", "text 2", ts, None, None, None),
        ],
        [],  # second call returns empty
    ]

    # Mock cursor for UPDATE
    update_cursor = MagicMock()
    update_cursor.rowcount = 1

    # Sequence: count query, then fetch, then updates, then fetch again
    conn.cursor.return_value.__enter__ = MagicMock(
        side_effect=[
            count_cursor,
            fetch_cursor,
            update_cursor,
            update_cursor,
            fetch_cursor,
        ]
    )
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    stats = run_backfill("postgresql://test", batch_size=10, concurrency=1)

    assert stats["total_processed"] == 2
    assert stats["total_summarized"] == 2
    assert stats["total_errors"] == 0


@patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"})
@patch("backfill_summaries.anthropic")
@patch("backfill_summaries.psycopg")
def test_run_backfill_with_limit(
    mock_psycopg: MagicMock,
    mock_anthropic: MagicMock,
) -> None:
    """Limit flag restricts number of rulings processed."""
    mock_client = MagicMock()
    mock_anthropic.Anthropic.return_value = mock_client
    content_block = MagicMock()
    content_block.text = "A summary."
    response = MagicMock()
    response.content = [content_block]
    mock_client.messages.create.return_value = response

    ts = datetime(2025, 1, 1)
    conn = MagicMock()
    mock_psycopg.connect.return_value.__enter__ = MagicMock(return_value=conn)
    mock_psycopg.connect.return_value.__exit__ = MagicMock(return_value=False)

    count_cursor = MagicMock()
    count_cursor.fetchone.return_value = (100,)

    fetch_cursor = MagicMock()
    fetch_cursor.fetchall.return_value = [("uuid-1", "text 1", ts, None, None, None)]

    update_cursor = MagicMock()
    update_cursor.rowcount = 1

    conn.cursor.return_value.__enter__ = MagicMock(
        side_effect=[count_cursor, fetch_cursor, update_cursor]
    )
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    stats = run_backfill("postgresql://test", batch_size=10, concurrency=1, limit=1)

    assert stats["total_processed"] == 1


@patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"})
@patch("backfill_summaries.anthropic")
@patch("backfill_summaries.psycopg")
def test_run_backfill_dry_run(
    mock_psycopg: MagicMock,
    mock_anthropic: MagicMock,
) -> None:
    """Dry run does not commit."""
    mock_client = MagicMock()
    mock_anthropic.Anthropic.return_value = mock_client
    content_block = MagicMock()
    content_block.text = "A summary."
    response = MagicMock()
    response.content = [content_block]
    mock_client.messages.create.return_value = response

    ts = datetime(2025, 1, 1)
    conn = MagicMock()
    mock_psycopg.connect.return_value.__enter__ = MagicMock(return_value=conn)
    mock_psycopg.connect.return_value.__exit__ = MagicMock(return_value=False)

    count_cursor = MagicMock()
    count_cursor.fetchone.return_value = (1,)

    fetch_cursor = MagicMock()
    fetch_cursor.fetchall.side_effect = [
        [("uuid-1", "text 1", ts, None, None, None)],
        [],
    ]

    conn.cursor.return_value.__enter__ = MagicMock(
        side_effect=[count_cursor, fetch_cursor, fetch_cursor]
    )
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    stats = run_backfill(
        "postgresql://test", batch_size=10, concurrency=1, dry_run=True
    )

    assert stats["total_summarized"] == 1
    conn.commit.assert_not_called()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_main_missing_database_url() -> None:
    """Main exits with error if DATABASE_URL is not set."""
    with patch.dict(os.environ, {}, clear=True):
        os.environ.pop("DATABASE_URL", None)
        with pytest.raises(SystemExit) as exc_info:
            with patch("sys.argv", ["backfill_summaries.py"]):
                backfill_summaries.main()
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# UPDATE query includes summary_model
# ---------------------------------------------------------------------------


def test_update_query_includes_model() -> None:
    """Verify the UPDATE query sets summary_model alongside summary."""
    assert "summary_model" in backfill_summaries.UPDATE_QUERY
    assert "summary_generated_at" in backfill_summaries.UPDATE_QUERY
