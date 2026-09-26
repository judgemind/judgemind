"""Tests for framework.search.ruling_doc — the shared search-doc builder (#4785)."""

from __future__ import annotations

from datetime import date, datetime
from unittest.mock import MagicMock

from framework.search import (
    build_search_event,
    fetch_ruling_search_rows,
    load_search_events,
    search_doc_metadata,
)
from framework.search.indexer import INDEXED_METADATA_FIELDS
from framework.search.ruling_doc import RULING_SEARCH_SQL

D1 = "aaaaaaaa-0000-0000-0000-000000000001"
D2 = "aaaaaaaa-0000-0000-0000-000000000002"
D3 = "aaaaaaaa-0000-0000-0000-000000000003"


def _conn_returning(*batches: list[tuple]) -> tuple[MagicMock, MagicMock]:
    cur = MagicMock()
    cols = []
    for name in ("document_id", "case_title"):
        col = MagicMock()
        col.name = name
        cols.append(col)
    cur.description = cols
    cur.fetchall.side_effect = list(batches)
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    return conn, cur


def test_sql_projects_every_indexed_field_from_derived() -> None:
    for field in INDEXED_METADATA_FIELDS:
        assert f"AS {field}" in RULING_SEARCH_SQL
    assert "derived.rulings" in RULING_SEARCH_SQL
    assert "c.case_type" in RULING_SEARCH_SQL
    assert "j.canonical_name" in RULING_SEARCH_SQL


def test_fetch_keys_by_document_and_first_row_wins() -> None:
    conn, cur = _conn_returning([(D1, "First"), (D1, "Second"), (D2, "Other")])

    rows = fetch_ruling_search_rows(conn, [D1, D2])

    assert rows == {
        D1: {"document_id": D1, "case_title": "First"},
        D2: {"document_id": D2, "case_title": "Other"},
    }
    sql, params = cur.execute.call_args.args
    assert "ANY(%s::uuid[])" in sql
    assert params == ([D1, D2],)


def test_fetch_drops_non_uuid_ids_and_batches() -> None:
    conn, cur = _conn_returning([(D1, "a"), (D2, "b")], [(D3, "c")])

    rows = fetch_ruling_search_rows(conn, [D1, "not-a-uuid", D2, D3], batch_size=2)

    assert set(rows) == {D1, D2, D3}
    assert [c.args[1] for c in cur.execute.call_args_list] == [([D1, D2],), ([D3],)]


def test_fetch_with_no_ids_runs_no_query() -> None:
    conn, cur = _conn_returning()
    assert fetch_ruling_search_rows(conn, ["junk"]) == {}
    cur.execute.assert_not_called()


def test_build_search_event_normalizes_date_and_fills_summary() -> None:
    row = {
        "document_id": D1,
        "hearing_date": date(2026, 7, 28),
        "ruling_text": "x" * 800,
        "summary": None,
        "case_type": "civil",
    }
    event = build_search_event(row)
    assert event["hearing_date"] == "2026-07-28"
    assert event["summary"] == "x" * 500
    assert event["case_type"] == "civil"
    assert row["hearing_date"] == date(2026, 7, 28)  # input not mutated


def test_build_search_event_keeps_stored_summary_and_handles_no_text() -> None:
    assert build_search_event({"document_id": D1, "summary": "Short."})["summary"] == "Short."
    event = build_search_event(
        {"document_id": D1, "ruling_text": None, "hearing_date": datetime(2026, 7, 28, 9)}
    )
    assert event["summary"] is None
    assert event["hearing_date"] == "2026-07-28"


def test_load_search_events_builds_each_row() -> None:
    conn, _ = _conn_returning([(D1, "Doe v. Roe")])
    events = load_search_events(conn, [D1])
    assert events == {
        D1: {"document_id": D1, "case_title": "Doe v. Roe", "summary": None, "hearing_date": None}
    }


def test_search_doc_metadata_matches_indexed_fields() -> None:
    meta = search_doc_metadata({"document_id": D1, "hearing_date": "2026-07-28T00:00:00"})
    assert tuple(meta) == INDEXED_METADATA_FIELDS
    assert meta["hearing_date"] == "2026-07-28"
    assert meta["content_hash"] == ""
