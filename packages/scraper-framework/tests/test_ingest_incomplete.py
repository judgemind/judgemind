"""Durable incomplete-key records (#5008).

Unit tests for ``ingestion.ingest_incomplete`` and the worker paths that
write or clear a record: the single-document fallback, a failed reingest, a
recorded skip, a whole-document write, and the dead-letter (which the split
harness does not drive).  The split paths (partial split, exhausted child,
complete split) are covered against a real Postgres by the split harness's
``marker`` invariant (``test_split_invariants_pg.py -k retry_marker``).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from ingestion import worker as worker_mod
from ingestion.ingest_incomplete import (
    INCOMPLETE_REASONS,
    clear_ingest_incomplete,
    record_ingest_incomplete,
)
from ingestion.worker import (
    INCOMPLETE_FALLBACK_KEY,
    NO_EXTRACTABLE_TEXT,
    REPLACE_SPLIT_SET_KEY,
    IngestionWorker,
    NoExtractableTextError,
    SplitExtractionFailedError,
    _incomplete_split_reason,
    _SplitBatch,
)

S3_KEY = "ca/orange/superior_court/raw/" + "a" * 64 + ".pdf"


def _conn() -> tuple[MagicMock, MagicMock]:
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    return conn, cur


def _sqls(cur: MagicMock) -> list[str]:
    return [c.args[0] for c in cur.execute.call_args_list]


def _incomplete_writes(cur: MagicMock) -> list[tuple[Any, ...]]:
    return [
        c.args[1]
        for c in cur.execute.call_args_list
        if "INSERT INTO telemetry.ingest_incomplete" in c.args[0]
    ]


def _incomplete_clears(cur: MagicMock) -> list[tuple[Any, ...]]:
    return [
        c.args[1]
        for c in cur.execute.call_args_list
        if "DELETE FROM telemetry.ingest_incomplete" in c.args[0]
    ]


# ---------------------------------------------------------------------------
# ingestion.ingest_incomplete
# ---------------------------------------------------------------------------


def test_retry_marker_record_upserts_under_a_savepoint() -> None:
    conn, cur = _conn()
    ok = record_ingest_incomplete(
        conn,
        s3_key=S3_KEY,
        reason="partial_split",
        county="Orange",
        scraper_id="ca-orange-tentatives",
        document_id="doc-1",
        detail={"failed_parts": [3]},
    )
    assert ok is True
    sqls = _sqls(cur)
    assert sqls[0] == "SAVEPOINT ingest_incomplete"
    assert "INSERT INTO telemetry.ingest_incomplete" in sqls[1]
    assert "occurrences = telemetry.ingest_incomplete.occurrences + 1" in sqls[1]
    assert sqls[2] == "RELEASE SAVEPOINT ingest_incomplete"
    params = cur.execute.call_args_list[1].args[1]
    assert params[:5] == (S3_KEY, "partial_split", "Orange", "ca-orange-tentatives", "doc-1")
    assert json.loads(params[5]) == {"failed_parts": [3]}
    conn.commit.assert_not_called()  # the caller owns the transaction


def test_retry_marker_record_without_key_or_with_unknown_reason_writes_nothing() -> None:
    conn, cur = _conn()
    assert record_ingest_incomplete(conn, s3_key=None, reason="dead_letter") is False
    assert record_ingest_incomplete(conn, s3_key="", reason="dead_letter") is False
    assert record_ingest_incomplete(conn, s3_key=S3_KEY, reason="made_up") is False
    cur.execute.assert_not_called()


def test_retry_marker_write_failure_rolls_back_to_savepoint_and_never_raises() -> None:
    conn, cur = _conn()

    def execute(sql: str, *_a: object) -> None:
        if "INSERT" in sql or "DELETE" in sql:
            raise RuntimeError('relation "telemetry.ingest_incomplete" does not exist')

    cur.execute.side_effect = execute
    assert record_ingest_incomplete(conn, s3_key=S3_KEY, reason="dead_letter") is False
    assert _sqls(cur)[-1] == "ROLLBACK TO SAVEPOINT ingest_incomplete"
    assert clear_ingest_incomplete(conn, S3_KEY) is False


def test_retry_marker_clear_failure_warns_once(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    from ingestion import ingest_incomplete

    conn, cur = _conn()

    def execute(sql: str, *_a: object) -> None:
        if "DELETE" in sql:
            raise RuntimeError('relation "telemetry.ingest_incomplete" does not exist')

    cur.execute.side_effect = execute
    with (
        patch.object(ingest_incomplete, "_clear_failure_logged", False),
        caplog.at_level(logging.DEBUG, logger="ingestion.ingest_incomplete"),
    ):
        assert clear_ingest_incomplete(conn, S3_KEY) is False
        assert clear_ingest_incomplete(conn, S3_KEY) is False
    levels = [r.levelno for r in caplog.records if "record clear failed" in r.getMessage()]
    assert levels == [logging.WARNING, logging.DEBUG]


def test_retry_marker_cursor_failure_never_raises() -> None:
    conn = MagicMock()
    conn.cursor.side_effect = RuntimeError("connection closed")
    assert record_ingest_incomplete(conn, s3_key=S3_KEY, reason="dead_letter") is False


def test_retry_marker_clear_deletes_the_key() -> None:
    conn, cur = _conn()
    assert clear_ingest_incomplete(conn, S3_KEY) is True
    assert _incomplete_clears(cur) == [(S3_KEY,)]
    assert clear_ingest_incomplete(conn, None) is False


def test_retry_marker_reasons_match_the_migration_comment() -> None:
    assert INCOMPLETE_REASONS == {
        "single_document_fallback",
        "partial_split",
        "exhausted_child",
        "dead_letter",
        "reingest_failed",
        "stale_validation",
    }


@pytest.mark.parametrize(
    ("batch", "expected"),
    [
        pytest.param(_SplitBatch("p"), None, id="complete"),
        pytest.param(_SplitBatch("p", keep_ids={"c"}), None, id="kept-sibling-not-exhausted"),
        pytest.param(_SplitBatch("p", exhausted_ids={"c"}), "exhausted_child", id="exhausted"),
        pytest.param(
            _SplitBatch("p", incomplete_reason="partial_split", exhausted_ids={"c"}),
            "partial_split",
            id="partial",
        ),
    ],
)
def test_retry_marker_split_reason(batch: _SplitBatch, expected: str | None) -> None:
    assert _incomplete_split_reason(batch) == expected


# ---------------------------------------------------------------------------
# Worker paths
# ---------------------------------------------------------------------------


def _worker() -> tuple[IngestionWorker, MagicMock, MagicMock, MagicMock]:
    redis = MagicMock()
    os_client = MagicMock()
    os_client.indices.exists.return_value = False
    worker = IngestionWorker(
        redis_client=redis,
        pg_dsn="postgresql://localhost/test",
        opensearch_client=os_client,
        s3_client=MagicMock(),
        archive_bucket="test-bucket",
    )
    conn, cur = _conn()
    worker._get_connection = lambda: conn  # type: ignore[method-assign]
    return worker, redis, conn, cur


def _event(**extra: Any) -> dict[str, Any]:
    return {
        "document_id": "doc-1",
        "state": "CA",
        "county": "Orange",
        "scraper_id": "ca-orange-tentatives",
        "s3_key": S3_KEY,
        **extra,
    }


def test_retry_marker_dead_letter_after_retries() -> None:
    """Dead-lettering is only an xack: the key is recorded first (#5008)."""
    worker, redis, conn, cur = _worker()
    worker._max_retries = 2
    order: list[str] = []
    conn.rollback.side_effect = lambda: order.append("rollback")
    conn.commit.side_effect = lambda: order.append("commit")
    redis.xack.side_effect = lambda *_a: order.append("xack")
    with patch.object(worker, "process_event", side_effect=ValueError("bad data")):
        worker._process_message(b"1-0", {b"data": json.dumps(_event()).encode()})
    writes = _incomplete_writes(cur)
    assert [w[:2] for w in writes] == [(S3_KEY, "dead_letter")]
    assert json.loads(writes[0][5]) == {"error": "ValueError: bad data", "attempts": 2}
    assert order == ["rollback", "commit", "xack"]


def test_retry_marker_dead_letter_on_schema_constraint_error() -> None:
    worker, redis, _conn_, cur = _worker()
    with (
        patch.object(worker, "process_event", side_effect=ValueError("violates check")),
        patch.object(worker_mod, "is_schema_constraint_error", return_value=True),
    ):
        worker._process_message(b"1-0", {b"data": json.dumps(_event()).encode()})
    writes = _incomplete_writes(cur)
    assert [w[:2] for w in writes] == [(S3_KEY, "dead_letter")]
    assert json.loads(writes[0][5])["attempts"] == 1
    redis.xack.assert_called_once()


def test_retry_marker_dead_letter_without_s3_key_or_malformed_records_nothing() -> None:
    worker, redis, _conn_, cur = _worker()
    worker._max_retries = 1
    with patch.object(worker, "process_event", side_effect=ValueError("bad")):
        worker._process_message(b"1-0", {b"data": json.dumps(_event(s3_key=None)).encode()})
        worker._process_message(b"2-0", {b"data": b"{not json"})
    assert _incomplete_writes(cur) == []
    assert redis.xack.call_count == 2


def test_retry_marker_dead_letter_record_failure_still_acks() -> None:
    worker, redis, conn, _cur = _worker()
    worker._max_retries = 1
    conn.cursor.side_effect = RuntimeError("connection closed")
    with patch.object(worker, "process_event", side_effect=ValueError("bad")):
        worker._process_message(b"1-0", {b"data": json.dumps(_event()).encode()})
    redis.xack.assert_called_once()


def test_retry_marker_single_document_fallback_recorded_before_the_write() -> None:
    worker, _redis, conn, cur = _worker()
    with patch.object(worker, "_key_has_rows", return_value=False):
        handled = worker._leave_key_after_failed_extraction(
            _event(), "doc-1", "Orange", "multimodal: PageCapExceededError"
        )
    assert handled is False  # the caller writes the document whole
    assert [w[:2] for w in _incomplete_writes(cur)] == [(S3_KEY, "single_document_fallback")]
    conn.commit.assert_called()
    assert worker._pending_incomplete_fallback is True


def test_retry_marker_live_key_with_rows_left_alone_records_nothing() -> None:
    worker, _redis, _conn_, cur = _worker()
    with patch.object(worker, "_key_has_rows", return_value=True):
        handled = worker._leave_key_after_failed_extraction(
            _event(), "doc-1", "Orange", "multimodal: boom"
        )
    assert handled is True
    assert _incomplete_writes(cur) == []
    assert _incomplete_clears(cur) == []


def test_retry_marker_reingest_failed_recorded() -> None:
    worker, _redis, conn, cur = _worker()
    order: list[str] = []
    conn.rollback.side_effect = lambda: order.append("rollback")
    conn.commit.side_effect = lambda: order.append("commit")
    with pytest.raises(SplitExtractionFailedError):
        worker._leave_key_after_failed_extraction(
            _event(**{REPLACE_SPLIT_SET_KEY: True}), "doc-1", "Orange", "text: boom"
        )
    assert [w[:2] for w in _incomplete_writes(cur)] == [(S3_KEY, "reingest_failed")]
    # The failed event's writes are rolled back first, as the raise would.
    assert order == ["rollback", "commit"]


def test_retry_marker_no_extractable_text_is_a_verdict_that_clears() -> None:
    worker, _redis, _conn_, cur = _worker()
    with pytest.raises(NoExtractableTextError):
        worker._leave_key_after_failed_extraction(
            _event(**{REPLACE_SPLIT_SET_KEY: True}), "doc-1", "Orange", NO_EXTRACTABLE_TEXT
        )
    assert _incomplete_writes(cur) == []
    assert _incomplete_clears(cur) == [(S3_KEY,)]


def _write(**event_extra: Any) -> SimpleNamespace:
    return SimpleNamespace(
        event_data=_event(**event_extra),
        s3_key=S3_KEY,
        document_id="doc-1",
        judge_vote=None,
        shown_changes=SimpleNamespace(judge_ids=set()),
        **{
            f: None
            for f in (
                "content_format",
                "content_hash",
                "s3_bucket",
                "source_url",
                "scraper_id",
                "captured_at",
                "hearing_dt",
                "ruling_text",
                "ruling_text_html",
                "department",
                "outcome",
                "motion_type",
                "summary",
                "summary_model",
                "summary_generated_at",
                "hearing_date_source",
            )
        },
    )


@pytest.mark.parametrize(
    ("event_extra", "cleared"),
    [
        pytest.param({}, True, id="whole-document-is-complete"),
        pytest.param({INCOMPLETE_FALLBACK_KEY: True}, False, id="fallback-keeps-record"),
        pytest.param({"_split_processed": True}, False, id="split-child-says-nothing"),
    ],
)
def test_retry_marker_whole_document_write(event_extra: dict[str, Any], cleared: bool) -> None:
    worker, _redis, _conn_, cur = _worker()
    with (
        patch.object(worker, "_prepare_ruling_write", return_value=("court", "case", None)),
        patch.object(worker, "_link_ruling_rows"),
        patch.object(worker, "_index_committed_ruling"),
        patch.object(worker, "_reindex_dependent_rulings"),
        patch.object(worker_mod, "insert_document_and_ruling"),
        patch.object(worker_mod, "record_judge_votes"),
        patch.object(worker_mod, "_should_move_off_unsupported_case", return_value=False),
    ):
        worker._write_one_ruling(_write(**event_extra))  # type: ignore[arg-type]
    assert bool(_incomplete_clears(cur)) is cleared
