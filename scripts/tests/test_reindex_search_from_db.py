"""Tests for scripts/reindex_search_from_db.py (#4712).

The script imports psycopg / framework only inside ``main()``, so the pure
helpers import cleanly in the lightweight scripts-tests environment.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, date, datetime
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import reindex_search_from_db as reindex


@pytest.mark.parametrize(
    ("value", "shape"),
    [
        ("2026-07-28", "date_only"),
        ("2026-07-28T00:00:00", "datetime"),
        ("2026-07-28 00:00:00", "datetime"),
        (None, "null"),
        ("", "null"),
        ("July 28", "other"),
        (20260728, "other"),
    ],
)
def test_classify_hearing_date(value: object, shape: str) -> None:
    assert reindex.classify_hearing_date(value) == shape


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-07-28", "2026-07-28"),
        ("2026-07-28T00:00:00", "2026-07-28"),
        (date(2026, 7, 28), "2026-07-28"),
        (datetime(2026, 7, 28, 9, tzinfo=UTC), "2026-07-28"),
        (None, None),
        ("", None),
        ("junk", None),
    ],
)
def test_date_part(value: object, expected: str | None) -> None:
    assert reindex.date_part(value) == expected


PG_ROW = {
    "document_id": "aad4f50a-26ad-5ee0-984d-0259fda7d54a",
    "case_number": "21STCV42883",
    "case_title": "Murillo v. BNSF Railway Company",
    "hearing_date": date(2026, 7, 28),
    "ruling_text": "x" * 800,
    "summary": None,
}


def test_drift_classes_in_sync() -> None:
    src = {
        "case_number": "21STCV42883",
        "case_title": "Murillo v. BNSF Railway Company",
        "hearing_date": "2026-07-28",
    }
    assert reindex.drift_classes(src, PG_ROW) == []


def test_drift_classes_issue_4712_sample() -> None:
    """The ruling from the issue: datetime date + stale title."""
    src = {
        "case_number": "21STCV42883",
        "case_title": "Berenice Murillo v. United Parcel Service, Inc",
        "hearing_date": "2026-07-28T00:00:00",
    }
    assert reindex.drift_classes(src, PG_ROW) == [
        "hearing_date_shape_datetime",
        "case_title_mismatch",
    ]


def test_drift_classes_date_and_number_mismatch() -> None:
    src = {
        "case_number": "OTHER",
        "case_title": PG_ROW["case_title"],
        "hearing_date": None,
    }
    assert reindex.drift_classes(src, PG_ROW) == [
        "hearing_date_mismatch",
        "case_number_mismatch",
    ]


def test_drift_classes_missing_and_orphan() -> None:
    assert reindex.drift_classes(None, PG_ROW) == ["missing_in_os"]
    assert reindex.drift_classes({"case_number": "X"}, None) == ["orphan_in_os"]
    assert reindex.drift_classes(None, None) == []


def test_build_event_normalizes_date_and_fills_summary() -> None:
    event = reindex.build_event(PG_ROW)
    assert event["hearing_date"] == "2026-07-28"
    assert event["summary"] == "x" * 500
    assert event["case_title"] == "Murillo v. BNSF Railway Company"
    assert event["document_id"] == PG_ROW["document_id"]


def test_build_event_keeps_existing_summary() -> None:
    event = reindex.build_event({**PG_ROW, "summary": "Short summary."})
    assert event["summary"] == "Short summary."


def test_build_event_empty_text_has_no_summary() -> None:
    event = reindex.build_event({**PG_ROW, "ruling_text": None})
    assert event["summary"] is None


def test_chunked() -> None:
    assert list(reindex.chunked(range(5), 2)) == [[0, 1], [2, 3], [4]]
    assert list(reindex.chunked([], 2)) == []


def test_fetch_pg_rows_first_ruling_wins() -> None:
    cur = MagicMock()
    col_a, col_b = MagicMock(), MagicMock()
    col_a.name, col_b.name = "document_id", "case_title"
    cur.description = [col_a, col_b]
    cur.fetchall.return_value = [("d1", "First"), ("d1", "Second"), ("d2", "Other")]
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur

    rows = reindex.fetch_pg_rows(conn, ["d1", "d2"])

    assert rows == {
        "d1": {"document_id": "d1", "case_title": "First"},
        "d2": {"document_id": "d2", "case_title": "Other"},
    }
    sql, params = cur.execute.call_args.args
    assert "ANY(%s)" in sql
    assert params == (["d1", "d2"],)


def test_fetch_pg_document_ids_county_filter() -> None:
    cur = MagicMock()
    cur.fetchall.return_value = [("d1",), ("d2",)]
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur

    assert reindex.fetch_pg_document_ids(conn, "Los Angeles") == ["d1", "d2"]
    sql, params = cur.execute.call_args.args
    assert "co.county = %s" in sql
    assert params == ("Los Angeles",)

    reindex.fetch_pg_document_ids(conn, None)
    sql, params = cur.execute.call_args.args
    assert "WHERE" not in sql
    assert params == ()


def test_main_requires_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("OPENSEARCH_URL", raising=False)
    monkeypatch.setitem(sys.modules, "psycopg", MagicMock())
    monkeypatch.setitem(sys.modules, "framework", MagicMock())
    monkeypatch.setitem(sys.modules, "framework.opensearch_client", MagicMock())

    assert reindex.main([]) == 2
