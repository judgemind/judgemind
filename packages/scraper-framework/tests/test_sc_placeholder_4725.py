"""Regression tests for #4725: Santa Clara "rulings not available" placeholders.

When a department has not posted its rulings yet, Santa Clara serves a
one-line placeholder PDF at the ruling URL:

    The tentative rulings are not available at this time. Please check back later.

Nothing recognised it, so the worker sent it to the LLM, which invented
rulings that the case-number rule then rejected as ``UNKNOWN-`` fails in
``telemetry.validation_results``.

Fixture ``sc_placeholder_not_available_4725.pdf`` is a real raw from dev S3
(``ca/santa_clara/superior_court/raw/4d5f577e2bb7...pdf``, the key with 8
``UNKNOWN-`` fails in the issue).  On 2026-11-12, 12 of the 485 SC raws in dev
S3 were this notice, every one extracting to exactly 78 characters; the next
shortest real document (a public notice) extracts to 1,150 characters.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
import respx

from courts.ca.sc_tentatives import (
    LANDING_URL,
    PLACEHOLDER_NOTICE_MAX_CHARS,
    SCTentativeRulingsScraper,
    extract_pdf_text,
    is_placeholder_notice,
)
from courts.ca.sc_tentatives import default_config as sc_default_config
from ingestion.worker import IngestionWorker

FIXTURES = Path(__file__).parent / "fixtures"
PLACEHOLDER_PDF = FIXTURES / "sc_placeholder_not_available_4725.pdf"
NOTICE = "The tentative rulings are not available at this time. Please check back later."

# Real SC ruling PDFs of every known layout: none of them is a placeholder.
REAL_RULING_PDFS = (
    "sc_dept1_tues.pdf",
    "sc_dept6_tues.pdf",
    "sc_dept16_wed.pdf",
    "sc_dept12_table_4681_a.pdf",
    "sc_table_4696_dept2.pdf",
    "sc_table_4696_dept7.pdf",
)


@pytest.fixture(autouse=True)
def _no_split_sibling_move() -> Iterator[None]:
    with patch("ingestion.db._move_split_sibling_ruling", return_value=False):
        yield


# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------


def test_sc_placeholder_real_fixture_is_detected() -> None:
    text = extract_pdf_text(PLACEHOLDER_PDF.read_bytes())
    assert text.strip() == NOTICE
    assert is_placeholder_notice(text)


@pytest.mark.parametrize("name", REAL_RULING_PDFS)
def test_sc_placeholder_real_rulings_are_not_placeholders(name: str) -> None:
    assert not is_placeholder_notice(extract_pdf_text((FIXTURES / name).read_bytes()))


@pytest.mark.parametrize(
    "text",
    [
        NOTICE,
        "  The tentative rulings are not available at this time.\nPlease check\nback later.\n",
        "THE TENTATIVE RULINGS ARE NOT AVAILABLE AT THIS TIME. PLEASE CHECK BACK LATER.",
        "The tentative ruling is not available at this time.",
    ],
)
def test_sc_placeholder_wording_and_whitespace_variants(text: str) -> None:
    assert is_placeholder_notice(text)


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        # A real ruling document that quotes the notice is not a placeholder:
        # the length threshold keeps it on the ruling path.
        NOTICE + " " + "Line 1 Case No.: 25CV111111 The motion is GRANTED. " * 10,
        # The notice must lead the document, not appear inside a ruling.
        "Line 1 Case No.: 25CV111111. " + NOTICE,
        "Tentative rulings for Department 6 are available below.",
    ],
)
def test_sc_placeholder_rejects_non_placeholders(text: str | None) -> None:
    assert not is_placeholder_notice(text)


def test_sc_placeholder_threshold_has_margin_over_the_real_notice() -> None:
    assert len(NOTICE) < PLACEHOLDER_NOTICE_MAX_CHARS < 1150


# ---------------------------------------------------------------------------
# Worker: live capture, rebuild and reingest all go through process_event
# ---------------------------------------------------------------------------


def _make_worker() -> tuple[IngestionWorker, MagicMock, MagicMock]:
    worker = IngestionWorker(
        redis_client=MagicMock(),
        pg_dsn="postgresql://localhost/test",
        opensearch_client=MagicMock(),
        s3_client=MagicMock(),
        archive_bucket="test-bucket",
    )
    worker._enrichment_client = None
    text_extractor = MagicMock()
    text_extractor.extract.side_effect = AssertionError("LLM called for a placeholder")
    multimodal = MagicMock()
    multimodal.extract_from_pdf.side_effect = AssertionError("LLM called for a placeholder")
    worker._get_framework_extractor = lambda: text_extractor  # type: ignore[method-assign]
    worker._get_county_extractor = lambda *a, **k: text_extractor  # type: ignore[method-assign]
    worker._get_multimodal_extractor = lambda **k: multimodal  # type: ignore[method-assign]
    worker._fetch_raw_pdf_from_s3 = MagicMock(  # type: ignore[method-assign]
        return_value=PLACEHOLDER_PDF.read_bytes()
    )
    worker._get_connection = MagicMock()  # type: ignore[method-assign]
    return worker, text_extractor, multimodal


def _sc_event(**overrides: object) -> dict:
    event: dict = {
        "document_id": "aaaaaaaa-0000-0000-0000-000000004725",
        "scraper_id": "ca-sc-tentatives-civil",
        "state": "CA",
        "county": "Santa Clara",
        "court": "Superior Court",
        "source_url": "https://santaclara.courts.ca.gov/system/files/tentative-ruling/d6.pdf",
        "content_format": "pdf",
        "content_hash": "4d5f577e2bb7f9226e6d3e5d281bf8edd2f0892d6e67a674e4fbfbd0e3a8928d",
        "s3_key": "ca/santa_clara/superior_court/raw/4d5f577e2bb7.pdf",
        "s3_bucket": "judgemind-document-archive-dev",
        "ruling_text": NOTICE,
        "capture_timestamp": "2026-07-21T18:46:47+00:00",
    }
    event.update(overrides)
    return event


@pytest.mark.parametrize(
    "ruling_text",
    [
        pytest.param(NOTICE, id="scraper-extracted-text"),
        pytest.param(PLACEHOLDER_PDF.read_bytes().decode("latin-1"), id="raw-pdf-binary"),
    ],
)
@patch("ingestion.worker.delete_stale_split_children", return_value=0)
@patch("ingestion.worker.write_split_rulings", return_value={})
@patch("ingestion.worker.insert_validation_result")
def test_sc_placeholder_worker_skips_without_llm_or_rulings(
    mock_validation: MagicMock,
    mock_write: MagicMock,
    mock_delete_stale: MagicMock,
    ruling_text: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    worker, text_extractor, multimodal = _make_worker()
    event = _sc_event(ruling_text=ruling_text)
    invented_id = "bbbbbbbb-0000-5000-8000-000000004725"

    def _remove_invented(*_args: object, **kwargs: object) -> int:
        kwargs["deleted_ids"].append(invented_id)  # type: ignore[union-attr]
        return 1

    mock_delete_stale.side_effect = _remove_invented
    worker._indexer = MagicMock()

    with caplog.at_level(logging.INFO, logger="ingestion.worker"):
        worker.process_event(event)

    # No LLM call of any kind.
    text_extractor.extract.assert_not_called()
    multimodal.extract_from_pdf.assert_not_called()
    # No ruling and no validation row is written.
    mock_validation.assert_not_called()
    for call in mock_write.call_args_list:
        assert call.kwargs["children"] == []
    # The key's split set is replaced by the empty one, so rulings an older
    # run invented from the notice (and their fail rows) are removed.
    mock_delete_stale.assert_called_once()
    assert mock_delete_stale.call_args.args[1] == event["s3_key"]
    assert mock_delete_stale.call_args.args[2] == []
    worker._indexer.delete_documents.assert_called_once_with([invented_id])
    removed = [r for r in caplog.records if "holds no rulings" in r.getMessage()]
    assert len(removed) == 1
    assert removed[0].levelno == logging.INFO
    assert removed[0].reason == "santa_clara_placeholder"
    # A distinct log event, not the generic orphan / zero-extraction warning.
    skipped = [r for r in caplog.records if r.getMessage() == "santa_clara_placeholder_skipped"]
    assert len(skipped) == 1
    assert skipped[0].s3_key == event["s3_key"]
    messages = [r.getMessage() for r in caplog.records]
    assert "Orphan document: no rulings extracted" not in messages
    assert not any("every child was rejected" in m for m in messages)


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"county": "Riverside", "scraper_id": "ca-riverside-tentatives"}, id="county"),
        pytest.param({"content_format": "html"}, id="format"),
    ],
)
def test_sc_placeholder_gate_is_santa_clara_pdf_only(
    overrides: dict, caplog: pytest.LogCaptureFixture
) -> None:
    """Another county's document (or a non-PDF) with the same words still
    takes its normal extraction path."""
    worker, text_extractor, _ = _make_worker()
    text_extractor.extract.side_effect = None
    text_extractor.extract.return_value = []
    event = _sc_event(**overrides)

    with caplog.at_level(logging.INFO, logger="ingestion.worker"):
        handled = worker._llm_split_document(
            event, event["document_id"], NOTICE, "CA", event["county"], raw_pdf_bytes=None
        )

    # The extraction found no rulings, so the document is the split's one
    # child (#4940), not a placeholder with none.
    assert handled is True
    text_extractor.extract.assert_called_once()
    assert "santa_clara_placeholder_skipped" not in [r.getMessage() for r in caplog.records]
    assert "Orphan document: no rulings extracted" in [r.getMessage() for r in caplog.records]


# ---------------------------------------------------------------------------
# Scraper: a placeholder is a legitimate "no rulings today", not a failed fetch
# ---------------------------------------------------------------------------


@respx.mock
def test_sc_placeholder_fetches_count_as_ok_not_failed() -> None:
    """Every PDF URL serving the notice is a quiet day: the run succeeds and
    the notice is still archived (archive-first)."""
    respx.get(LANDING_URL).mock(
        return_value=httpx.Response(200, text=(FIXTURES / "sc_landing_page.html").read_text())
    )
    respx.get(url__regex=r"tentative-rulings/dep").mock(
        return_value=httpx.Response(200, text=(FIXTURES / "sc_dept1_page.html").read_text())
    )
    respx.get(url__regex=r"\.pdf$").mock(
        return_value=httpx.Response(200, content=PLACEHOLDER_PDF.read_bytes())
    )

    config = sc_default_config()
    config.request_delay_seconds = 0
    scraper = SCTentativeRulingsScraper(config=config)
    docs = scraper.fetch_documents()

    assert len(docs) == 20
    assert all(is_placeholder_notice(scraper.parse_document(d).ruling_text) for d in docs)
