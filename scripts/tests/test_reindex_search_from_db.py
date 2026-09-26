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


# Expected metadata as ``expected_metadata`` returns it: the search doc built
# from the Postgres ruling (hearing_date already normalized to YYYY-MM-DD).
EXPECTED = {
    "case_number": "21STCV42883",
    "case_title": "Murillo v. BNSF Railway Company",
    "case_type": "civil",
    "judge_name": "Doe, Jane",
    "hearing_date": "2026-07-28",
    "motion_type": "demurrer",
    "outcome": None,
    "summary": "x" * 500,
}


def test_drift_classes_in_sync() -> None:
    assert reindex.drift_classes(dict(EXPECTED), EXPECTED) == []


def test_drift_classes_treats_empty_and_missing_as_equal() -> None:
    src = {**EXPECTED, "outcome": ""}
    src.pop("motion_type")
    assert reindex.drift_classes(src, {**EXPECTED, "motion_type": None}) == []


def test_drift_classes_issue_4712_sample() -> None:
    """The ruling from #4712: datetime date + stale title."""
    src = {
        **EXPECTED,
        "case_title": "Berenice Murillo v. United Parcel Service, Inc",
        "hearing_date": "2026-07-28T00:00:00",
    }
    assert reindex.drift_classes(src, EXPECTED) == [
        "hearing_date_shape_datetime",
        "case_title_mismatch",
    ]


def test_drift_classes_date_and_number_mismatch() -> None:
    src = {**EXPECTED, "case_number": "OTHER", "hearing_date": None}
    assert reindex.drift_classes(src, EXPECTED) == [
        "case_number_mismatch",
        "hearing_date_mismatch",
    ]


def test_drift_classes_issue_4785_worker_drift() -> None:
    """#4785: the pre-fix worker wrote raw event values — no case_type, the
    extracted judge name, a re-extracted motion type."""
    src = {**EXPECTED, "judge_name": "J. Doe", "motion_type": "other"}
    src.pop("case_type")
    assert reindex.drift_classes(src, EXPECTED) == [
        "case_type_mismatch",
        "judge_name_mismatch",
        "motion_type_mismatch",
    ]


def test_drift_classes_missing_and_orphan() -> None:
    assert reindex.drift_classes(None, EXPECTED) == ["missing_in_os"]
    assert reindex.drift_classes({"case_number": "X"}, None) == ["orphan_in_os"]
    assert reindex.drift_classes(None, None) == []


def test_chunked() -> None:
    assert list(reindex.chunked(range(5), 2)) == [[0, 1], [2, 3], [4]]
    assert list(reindex.chunked([], 2)) == []


def test_load_events_and_expected_metadata_delegate_to_framework(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The script builds docs only through framework.search (shared with the
    worker, #4785); it has no search-doc logic of its own."""
    ruling_doc = MagicMock()
    ruling_doc.load_search_events.return_value = {"d1": {"document_id": "d1"}}
    indexer = MagicMock()
    indexer.search_doc_metadata.return_value = {"case_number": "X"}
    monkeypatch.setitem(sys.modules, "framework", MagicMock())
    monkeypatch.setitem(sys.modules, "framework.search", MagicMock())
    monkeypatch.setitem(sys.modules, "framework.search.ruling_doc", ruling_doc)
    monkeypatch.setitem(sys.modules, "framework.search.indexer", indexer)

    conn = MagicMock()
    assert reindex.load_events(conn, ["d1"]) == {"d1": {"document_id": "d1"}}
    ruling_doc.load_search_events.assert_called_once_with(conn, ["d1"])
    assert reindex.expected_metadata({"document_id": "d1"}) == {"case_number": "X"}


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
