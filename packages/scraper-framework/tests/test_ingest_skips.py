"""Unit tests for ingestion.ingest_skips and the worker's empty-split reason (#5015)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from ingestion.ingest_skips import (
    SKIP_NO_RULING_TEXT,
    SKIP_REASONS,
    SKIP_SANTA_CLARA_PLACEHOLDER,
    record_ingest_skip,
)
from ingestion.worker import _empty_split_skip_reason, _SplitBatch


def _conn() -> tuple[MagicMock, MagicMock]:
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    return conn, cur


def test_skip_reason_record_upserts_under_a_savepoint() -> None:
    conn, cur = _conn()
    ok = record_ingest_skip(
        conn,
        s3_key="ca/santa_clara/superior_court/raw/abc.pdf",
        reason=SKIP_SANTA_CLARA_PLACEHOLDER,
        county="Santa Clara",
        scraper_id="ca-sc-tentatives-civil",
        document_id="doc-1",
        detail={"removed_rows": 2},
    )
    assert ok is True
    sqls = [c.args[0] for c in cur.execute.call_args_list]
    assert sqls[0] == "SAVEPOINT ingest_skip"
    assert "INSERT INTO telemetry.ingest_skips" in sqls[1]
    assert "ON CONFLICT (s3_key) DO UPDATE" in sqls[1]
    assert sqls[2] == "RELEASE SAVEPOINT ingest_skip"
    params = cur.execute.call_args_list[1].args[1]
    assert params[:5] == (
        "ca/santa_clara/superior_court/raw/abc.pdf",
        "santa_clara_placeholder",
        "Santa Clara",
        "ca-sc-tentatives-civil",
        "doc-1",
    )
    assert json.loads(params[5]) == {"removed_rows": 2}
    # The caller owns the transaction.
    conn.commit.assert_not_called()


def test_skip_reason_record_without_s3_key_writes_nothing() -> None:
    conn, cur = _conn()
    assert record_ingest_skip(conn, s3_key=None, reason=SKIP_NO_RULING_TEXT) is False
    assert record_ingest_skip(conn, s3_key="", reason=SKIP_NO_RULING_TEXT) is False
    cur.execute.assert_not_called()


def test_skip_reason_record_unknown_reason_is_not_written() -> None:
    conn, cur = _conn()
    assert record_ingest_skip(conn, s3_key="k", reason="made_up") is False
    cur.execute.assert_not_called()


def test_skip_reason_record_failure_rolls_back_to_savepoint_and_never_raises() -> None:
    conn, cur = _conn()

    def execute(sql: str, *_a: object) -> None:
        if "INSERT" in sql:
            raise RuntimeError('relation "telemetry.ingest_skips" does not exist')

    cur.execute.side_effect = execute
    assert record_ingest_skip(conn, s3_key="k", reason=SKIP_NO_RULING_TEXT) is False
    sqls = [c.args[0] for c in cur.execute.call_args_list]
    assert sqls[-1] == "ROLLBACK TO SAVEPOINT ingest_skip"


def test_skip_reason_record_cursor_failure_never_raises() -> None:
    conn = MagicMock()
    conn.cursor.side_effect = RuntimeError("connection closed")
    assert record_ingest_skip(conn, s3_key="k", reason=SKIP_NO_RULING_TEXT) is False


def test_skip_reasons_match_the_migration_comment() -> None:
    assert SKIP_REASONS == {
        "governor_bio_route",
        "santa_clara_placeholder",
        "zero_rulings_no_text",
        "no_extractable_text",
        "no_ruling_text",
        "raw_missing",
    }


@pytest.mark.parametrize(
    ("batch", "expected"),
    [
        pytest.param(
            _SplitBatch("p", no_rulings_reason="zero_rulings_no_text"),
            "zero_rulings_no_text",
            id="no-rulings-reason",
        ),
        pytest.param(_SplitBatch("p", textless_rows=3), "no_ruling_text", id="textless-only"),
        pytest.param(_SplitBatch("p"), None, id="children-rejected-elsewhere"),
        pytest.param(_SplitBatch("p", textless_rows=1, keep_ids={"c"}), None, id="kept-child"),
        pytest.param(
            _SplitBatch("p", textless_rows=1, children=[MagicMock()]), None, id="written-child"
        ),
        pytest.param(
            _SplitBatch("p", textless_rows=1, pre_split=MagicMock()), None, id="pre-split"
        ),
    ],
)
def test_skip_reason_for_empty_split(batch: _SplitBatch, expected: str | None) -> None:
    assert _empty_split_skip_reason(batch) == expected
