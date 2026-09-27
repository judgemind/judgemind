"""Structured-source hearing dates and the 180-day rule (#4793, #4755 option 2).

``hearing_date_in_range`` drops a ruling whose hearing date is more than 180
days from ``captured_at``.  For a date from a structured source (the
scraper's labelled header / filename / listing, the ``hearing_date_for_raw``
hook, the CC portal PDF header) the rule only flags; a sanity floor still
rejects years before 2000 and dates more than a year after capture.  Dates
from the LLM, a splitter's body parse, or the regex fallback keep the
180-day rule.  The worker carries ``hearing_date_source`` from the point a
date is assigned to the ruling row and the validation row.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from ingestion.worker import IngestionWorker
from validation.deterministic import check_hearing_date_in_range, run_deterministic_rules
from validation.hearing_date_source import (
    LLM,
    REGEX_FALLBACK,
    SPLITTER,
    STRUCTURED_HEADER,
    STRUCTURED_HOOK,
    STRUCTURED_SCRAPER,
    is_structured,
)

CAPTURED = date(2026, 9, 26)
OLD = date(2026, 1, 16)  # 253 days before CAPTURED — the Orange f883c2d8 case


# ---------------------------------------------------------------------------
# The rule itself
# ---------------------------------------------------------------------------


class TestIsStructured:
    @pytest.mark.parametrize("source", [STRUCTURED_SCRAPER, STRUCTURED_HOOK, STRUCTURED_HEADER])
    def test_structured_sources(self, source: str) -> None:
        assert is_structured(source)

    @pytest.mark.parametrize("source", [LLM, REGEX_FALLBACK, SPLITTER, None, "", "bogus"])
    def test_other_sources(self, source: str | None) -> None:
        assert not is_structured(source)


class TestHearingDateInRangeBySource:
    @pytest.mark.parametrize("source", [STRUCTURED_SCRAPER, STRUCTURED_HOOK, STRUCTURED_HEADER])
    def test_structured_old_date_is_flagged_not_failed(self, source: str) -> None:
        result = check_hearing_date_in_range(OLD, CAPTURED, hearing_date_source=source)
        assert result.rule == "hearing_date_in_range"
        assert result.result == "flag"
        assert "253 days" in (result.reason or "")
        assert source in (result.reason or "")

    @pytest.mark.parametrize("source", [LLM, REGEX_FALLBACK, SPLITTER, None])
    def test_unstructured_old_date_still_fails(self, source: str | None) -> None:
        result = check_hearing_date_in_range(OLD, CAPTURED, hearing_date_source=source)
        assert result.result == "fail"
        assert "exceeds 180-day threshold" in (result.reason or "")

    def test_default_source_keeps_the_180_day_rule(self) -> None:
        assert check_hearing_date_in_range(OLD, CAPTURED).result == "fail"

    def test_structured_in_range_passes(self) -> None:
        result = check_hearing_date_in_range(
            date(2026, 9, 20), CAPTURED, hearing_date_source=STRUCTURED_SCRAPER
        )
        assert result.result == "pass"

    def test_structured_year_before_2000_fails(self) -> None:
        result = check_hearing_date_in_range(
            date(1999, 12, 31), CAPTURED, hearing_date_source=STRUCTURED_HEADER
        )
        assert result.result == "fail"
        assert "1999-12-31" in (result.reason or "")

    def test_structured_year_2000_is_only_flagged(self) -> None:
        result = check_hearing_date_in_range(
            date(2000, 1, 1), CAPTURED, hearing_date_source=STRUCTURED_HEADER
        )
        assert result.result == "flag"

    def test_structured_more_than_a_year_ahead_fails(self) -> None:
        result = check_hearing_date_in_range(
            date(2027, 10, 1), CAPTURED, hearing_date_source=STRUCTURED_SCRAPER
        )
        assert result.result == "fail"

    def test_structured_under_a_year_ahead_is_flagged(self) -> None:
        result = check_hearing_date_in_range(
            date(2027, 6, 1), CAPTURED, hearing_date_source=STRUCTURED_SCRAPER
        )
        assert result.result == "flag"

    def test_structured_floor_applies_without_captured_at(self) -> None:
        result = check_hearing_date_in_range(
            date(1972, 9, 13), None, hearing_date_source=STRUCTURED_HOOK
        )
        assert result.result == "fail"

    def test_run_deterministic_rules_threads_the_source(self) -> None:
        kwargs: dict[str, Any] = {
            "ruling_text": "The motion for summary judgment is GRANTED.",
            "case_number": "C22-01081",
            "case_title": "Alpha v. Beta",
            "hearing_date": OLD,
            "captured_at": CAPTURED,
        }
        assert run_deterministic_rules(**kwargs).overall == "fail"
        assert (
            run_deterministic_rules(**kwargs, hearing_date_source=STRUCTURED_HEADER).overall
            == "flag"
        )


# ---------------------------------------------------------------------------
# Worker: source carried from assignment to the write
# ---------------------------------------------------------------------------


def _make_event(**overrides: object) -> dict:
    base: dict = {
        "document_id": "aaaaaaaa-0000-0000-0000-000000004793",
        "scraper_id": "ca-oc-tentatives-probate",
        "state": "CA",
        "county": "Orange",
        "court": "Superior Court",
        "source_url": "https://www.occourts.org/tentatives/probate.pdf",
        "content_format": "html",
        "content_hash": "f883c2d8",
        "s3_key": "ca/orange/superior_court/raw/f883c2d8.pdf",
        "s3_bucket": "judgemind-document-archive-dev",
        "case_number": "30-2025-01400000-PR-TR-CJC",
        "case_title": "In re the Alpha Trust",
        "department": "C08",
        "judge_name": "Smith, John A.",
        "ruling_text": "The petition is GRANTED as prayed. " * 5,
        "hearing_date": OLD.isoformat(),
        "capture_timestamp": "2026-09-26T13:44:35",
    }
    base.update(overrides)
    return base


def _make_worker() -> IngestionWorker:
    os_mock = MagicMock()
    os_mock.indices.exists.return_value = False
    with patch.dict("os.environ", {}):
        with patch("ingestion.worker.create_llm_client", return_value=None):
            return IngestionWorker(
                redis_client=MagicMock(),
                pg_dsn="postgresql://localhost/test",
                opensearch_client=os_mock,
                s3_client=MagicMock(),
                archive_bucket="test-bucket",
                llm_client=None,
            )


def _det_calls(mock_insert_validation: MagicMock) -> list:
    return [
        c
        for c in mock_insert_validation.call_args_list
        if c.kwargs["result"].model == "deterministic"
    ]


_SPLIT_MOCK = "ingestion.worker.IngestionWorker._llm_split_document"


@pytest.fixture
def db() -> Any:
    """Patch every DB touchpoint the single-document write path uses."""
    with (
        patch("ingestion.worker.psycopg") as mock_psycopg,
        patch("ingestion.worker.insert_validation_result") as mock_insert_validation,
        patch("ingestion.worker.insert_document_and_ruling", return_value=True) as mock_write,
        patch("ingestion.worker.upsert_court", return_value="court-uuid"),
        patch(
            "ingestion.worker.upsert_case_returning_title",
            return_value=("case-uuid", "In re the Alpha Trust"),
        ),
        patch("ingestion.worker.resolve_judge", return_value="judge-uuid"),
        patch("ingestion.worker.upsert_case_judge"),
        patch("ingestion.worker.batch_upsert_parties"),
        patch("ingestion.worker.EnrichmentEngine"),
        patch("ingestion.worker.extract_fields_llm", return_value=None),
    ):
        mock_psycopg.connect.return_value = MagicMock(closed=False)
        yield {"validation": mock_insert_validation, "write": mock_write}


@patch(_SPLIT_MOCK, return_value=False)
def test_structured_scraper_date_over_180_days_is_written_with_a_flag(
    _split: MagicMock, db: dict
) -> None:
    """A live capture's date comes from the scraper's labelled header."""
    _make_worker().process_event(_make_event())

    db["write"].assert_called_once()
    assert db["write"].call_args.kwargs["hearing_date"] == OLD
    assert db["write"].call_args.kwargs["hearing_date_source"] == STRUCTURED_SCRAPER
    det = _det_calls(db["validation"])
    assert len(det) == 1
    assert det[0].kwargs["result"].result == "flag"
    assert "hearing_date" in (det[0].kwargs["result"].reason or "")
    assert det[0].kwargs["hearing_date_source"] == STRUCTURED_SCRAPER


@patch(_SPLIT_MOCK, return_value=False)
def test_structured_date_before_2000_is_dropped(_split: MagicMock, db: dict) -> None:
    _make_worker().process_event(_make_event(hearing_date="1972-09-13"))

    db["write"].assert_not_called()
    det = _det_calls(db["validation"])
    assert det[0].kwargs["result"].result == "fail"
    assert det[0].kwargs["hearing_date_source"] == STRUCTURED_SCRAPER


@patch(_SPLIT_MOCK, return_value=False)
def test_hook_date_is_labelled_structured_hook(_split: MagicMock, db: dict) -> None:
    """Prefix reingest / rebuild events get the date from the scraper hook."""
    event = _make_event(hearing_date=None, scraper_id="reingest-ca-orange")
    with patch("ingestion.worker.raw_hearing_date", return_value=OLD.isoformat()):
        _make_worker().process_event(event)

    db["write"].assert_called_once()
    assert db["write"].call_args.kwargs["hearing_date_source"] == STRUCTURED_HOOK
    assert _det_calls(db["validation"])[0].kwargs["result"].result == "flag"


@patch(_SPLIT_MOCK, return_value=False)
def test_regex_fallback_date_over_180_days_is_still_dropped(_split: MagicMock, db: dict) -> None:
    event = _make_event(
        hearing_date=None,
        ruling_text="Hearing Date: January 16, 2026\n" + "The petition is GRANTED. " * 5,
    )
    with patch("ingestion.worker.raw_hearing_date", return_value=None):
        _make_worker().process_event(event)

    db["write"].assert_not_called()
    det = _det_calls(db["validation"])
    assert det[0].kwargs["result"].result == "fail"
    assert det[0].kwargs["hearing_date_source"] == REGEX_FALLBACK


@patch(_SPLIT_MOCK, return_value=False)
def test_llm_labelled_split_child_over_180_days_is_still_dropped(
    _split: MagicMock, db: dict
) -> None:
    child = _make_event(
        _split_processed=True,
        _llm_extracted=True,
        _split_index=0,
        _split_count=1,
        hearing_date_source=LLM,
    )
    _make_worker().process_event(child)

    db["write"].assert_not_called()
    det = _det_calls(db["validation"])
    assert det[0].kwargs["result"].result == "fail"
    assert det[0].kwargs["hearing_date_source"] == LLM


@patch(_SPLIT_MOCK, return_value=False)
def test_regex_date_does_not_become_structured(_split: MagicMock, db: dict) -> None:
    """A rebuild event pre-filled by the regex fallback keeps its label."""
    event = _make_event(hearing_date_source=REGEX_FALLBACK)
    _make_worker().process_event(event)

    db["write"].assert_not_called()
    assert _det_calls(db["validation"])[0].kwargs["hearing_date_source"] == REGEX_FALLBACK


# ---------------------------------------------------------------------------
# Split children: an LLM value never overwrites a structured date
# ---------------------------------------------------------------------------


def _converted(hearing_dates: list[str | None]) -> list:
    from ingestion.ruling_guards import ConvertedRuling

    return [
        ConvertedRuling(
            document_id=f"split-uuid-{i}",
            original_document_id="parent-uuid",
            split_index=i,
            split_count=len(hearing_dates),
            is_multi=len(hearing_dates) > 1,
            ruling_text=f"Ruling {i}: the petition is GRANTED as prayed.",
            case_number=f"30-2025-0140000{i}-PR-TR-CJC",
            case_title=f"In re Trust {i}",
            judge_name="Smith, John A.",
            department="C08",
            motion_type="petition",
            outcome="granted",
            hearing_date=hd,
        )
        for i, hd in enumerate(hearing_dates)
    ]


def _run_llm_split(event: dict, hearing_dates: list[str | None]) -> list[dict]:
    worker = _make_worker()
    extractor = MagicMock()
    extractor.extract.return_value = [MagicMock()] * len(hearing_dates)
    worker._framework_extractor = extractor
    children: list[dict] = []
    with (
        patch("ingestion.worker.convert_extracted_rulings", return_value=_converted(hearing_dates)),
        patch("ingestion.worker.delete_stale_split_children", return_value=0),
        patch("ingestion.worker.insert_validation_result"),
        patch("ingestion.worker.psycopg"),
        patch.object(worker, "process_event", side_effect=children.append),
    ):
        assert worker._llm_split_document(
            event, event["document_id"], event["ruling_text"], "CA", "Los Angeles"
        )
    return children


def test_llm_split_keeps_the_parent_structured_date_and_label() -> None:
    parent = _make_event(
        county="Los Angeles",
        scraper_id="ca-la-tentatives-civil",
        hearing_date_source=STRUCTURED_HEADER,
    )
    children = _run_llm_split(parent, ["2026-09-20", None])

    assert [c["hearing_date"] for c in children] == [OLD.isoformat(), OLD.isoformat()]
    assert [c["hearing_date_source"] for c in children] == [STRUCTURED_HEADER] * 2


def test_llm_split_date_is_labelled_llm_when_the_parent_has_none() -> None:
    parent = _make_event(
        county="Los Angeles",
        scraper_id="ca-la-tentatives-civil",
        hearing_date=None,
    )
    children = _run_llm_split(parent, ["2026-01-16", None])

    assert children[0]["hearing_date"] == "2026-01-16"
    assert children[0]["hearing_date_source"] == LLM
    assert children[1]["hearing_date"] is None
    assert children[1]["hearing_date_source"] is None


# ---------------------------------------------------------------------------
# Deterministic splitters: a per-entry body date is not structured
# ---------------------------------------------------------------------------


def test_deterministic_split_child_date_is_labelled_splitter() -> None:
    from ingestion.worker import _child_hearing_date

    parent = {"hearing_date": "2026-09-01", "hearing_date_source": STRUCTURED_HOOK}
    assert _child_hearing_date("2025-10-30", parent) == ("2025-10-30", SPLITTER)
    assert _child_hearing_date("2026-09-01", parent) == ("2026-09-01", STRUCTURED_HOOK)
    assert _child_hearing_date(None, parent) == ("2026-09-01", STRUCTURED_HOOK)
    assert _child_hearing_date(None, {}) == (None, None)


# ---------------------------------------------------------------------------
# DB helpers write the column
# ---------------------------------------------------------------------------


def _mock_conn() -> tuple[MagicMock, MagicMock]:
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return conn, cur


def test_insert_validation_result_writes_hearing_date_source() -> None:
    from validation.gate import ValidationResult, insert_validation_result

    conn, cur = _mock_conn()
    insert_validation_result(
        conn,
        document_id="doc",
        ruling_id=None,
        result=ValidationResult(
            result="flag",
            reason="r",
            model="deterministic",
            input_tokens=0,
            output_tokens=0,
            latency_ms=0,
        ),
        county="Orange",
        scraper_id="ca-oc-tentatives-probate",
        s3_key="k",
        hearing_date_source=STRUCTURED_SCRAPER,
    )
    sql, params = cur.execute.call_args.args
    assert "hearing_date_source" in sql
    assert params[-1] == STRUCTURED_SCRAPER


@pytest.mark.parametrize("force_update", [False, True])
def test_insert_ruling_writes_hearing_date_source(force_update: bool) -> None:
    from ingestion.db import insert_ruling

    conn, cur = _mock_conn()
    insert_ruling(
        conn,
        document_id="doc",
        case_id="case",
        court_id="court",
        hearing_date=OLD,
        ruling_text="text",
        department="C08",
        hearing_date_source=STRUCTURED_HOOK,
        force_update=force_update,
    )
    insert = next(c for c in cur.execute.call_args_list if "INSERT INTO rulings" in c.args[0])
    sql, params = insert.args
    assert "hearing_date_source" in sql
    assert STRUCTURED_HOOK in params
    # The source follows the date: an incoming NULL date keeps the old source.
    if not force_update:
        assert "WHEN EXCLUDED.hearing_date IS NULL THEN rulings.hearing_date_source" in sql
