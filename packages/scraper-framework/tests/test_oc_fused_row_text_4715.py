"""Regression tests for #4715: a fused OC row's second ruling stored under the first case.

The multimodal LLM sometimes returns two adjacent calendar rows as one row:
both captions in ``case_info`` and both cells' text in ``ruling_text``.  The
fused-row split (#2500) splits the captions but gave the whole text to the
first sub-case, so the second case's ruling was stored under the first case.

Fixture ``oc_gaffney_n16_fused_row.pdf`` is a real OC Dept N16 PDF (Judge
Gaffney, dev S3 ``ca/orange/superior_court/raw/4d92bae3...pdf``).  In the PDF,
entry 2 (Herrera vs. Bodda-Herrera) is OFF CALENDAR and entry 3 (Mosqueda vs.
Ford Motor Company) holds a demurrer ruling (OVERRULED).  Entries 8 (LCY
Partnership, OFF CALENDAR) and 9 (Rose vs. 1, CONTINUED TO 12/16/26) were fused
the same way.

* ``oc_gaffney_n16_fused_row_llm_rulings.json`` is the dev document-level LLM
  cache entry for that PDF, written before this fix.
* ``oc_gaffney_n16_fused_row_page_rows.json`` is the parsed per-page LLM rows
  (dev per-page cache) that ``_join_page_rows`` joins on a fresh extraction.
* ``oc_cadence_lost_caption_llm_rulings.json`` is the dev cache entry for dev
  S3 ``ca/orange/superior_court/raw/15bb9e58...pdf``.  There the LLM dropped
  entry 13's caption (Carrillo vs. Bryant) entirely, and its ruling followed
  entry 12's "OFF CALENDAR" in the Cadence Bank row.
* ``oc_cadence_repeated_caption_page_rows.json`` is a later fresh extraction
  of the same PDF (the cache entry above expired) from the dev per-page cache.
  This time the LLM copied "Cadence Bank N.A. vs. Richardson Carrillo vs.
  Bryant" onto both entry 12 ("OFF CALENDAR") and entry 13 (the ruling).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pdfplumber
import pytest

from framework.llm_extractor import (
    _apply_pdf_cache_hit_filters,
    _apply_pdf_post_join_filters,
    _assign_repeated_fused_captions,
    _join_page_rows,
    _second_caption,
    _split_fused_row_texts,
    _split_leading_stub_segments,
)
from framework.llm_schema import ExtractedRuling
from ingestion.split_ids import make_split_document_id
from ingestion.worker import IngestionWorker

FIXTURES = Path(__file__).parent / "fixtures"
N16_PDF = FIXTURES / "oc_gaffney_n16_fused_row.pdf"
N16_RULINGS = FIXTURES / "oc_gaffney_n16_fused_row_llm_rulings.json"
N16_PAGE_ROWS = FIXTURES / "oc_gaffney_n16_fused_row_page_rows.json"
CADENCE_RULINGS = FIXTURES / "oc_cadence_lost_caption_llm_rulings.json"
CADENCE_PAGE_ROWS = FIXTURES / "oc_cadence_repeated_caption_page_rows.json"

# A phrase that appears only in the Mosqueda ruling.
MOSQUEDA_DISPOSITION = "Giselle Mosqueda"


def _load(path: Path) -> list[ExtractedRuling]:
    return [ExtractedRuling(**row) for row in json.loads(path.read_text())]


def _ruling(
    *,
    text: str | None,
    entry: int | None = None,
    title: str | None = "Smith vs. Jones",
    case_number: str | None = None,
) -> ExtractedRuling:
    return ExtractedRuling(
        extracted_case_number=case_number,
        extracted_case_title=title,
        ruling_text=text,
        entry_number=entry,
    )


def _by_title(rulings: list[ExtractedRuling], prefix: str) -> ExtractedRuling:
    return next(r for r in rulings if (r.extracted_case_title or "").startswith(prefix))


# ---------------------------------------------------------------------------
# Ground truth and bug shape
# ---------------------------------------------------------------------------


def test_fused_row_text_fixture_pdf_prints_the_entries_as_separate_rows() -> None:
    with pdfplumber.open(N16_PDF) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    assert "2 Herrera vs. OFF CALENDAR\nBodda-Herrera\n3 Mosqueda vs. TENTATIVE RULING:" in text
    assert "Giselle Mosqueda" in text
    assert "8 LCY OFF CALENDAR" in text
    assert "9 Rose vs. 1 CONTINUED TO 12/16/26" in text


def test_fused_row_text_cached_rows_reproduce_the_bug_shape() -> None:
    """Before the fix: Herrera holds the Mosqueda ruling, Mosqueda has no text."""
    rows = _load(N16_RULINGS)
    herrera = _by_title(rows, "Herrera vs. Bodda")
    mosqueda = _by_title(rows, "Herrera Mosqueda vs. Ford")
    assert herrera.entry_number == 2
    assert herrera.ruling_text.startswith("OFF CALENDAR")
    assert MOSQUEDA_DISPOSITION in herrera.ruling_text
    assert mosqueda.entry_number is None
    assert not mosqueda.ruling_text


# ---------------------------------------------------------------------------
# AC 1: the Mosqueda ruling is not stored under Herrera
# ---------------------------------------------------------------------------


def test_fused_row_text_herrera_does_not_get_the_mosqueda_ruling() -> None:
    out = _apply_pdf_cache_hit_filters(_load(N16_RULINGS), content_key="4d92bae3" * 8)

    herrera = _by_title(out, "Herrera vs. Bodda")
    mosqueda = _by_title(out, "Herrera Mosqueda vs. Ford")
    assert not herrera.ruling_text
    assert MOSQUEDA_DISPOSITION in mosqueda.ruling_text
    assert mosqueda.ruling_text.startswith("**TENTATIVE RULING:**")
    assert "OFF CALENDAR" not in mosqueda.ruling_text
    # Entries 8 and 9 are both no-ruling stubs: neither row holds text.
    assert not _by_title(out, "LCY Partnership").ruling_text
    assert not _by_title(out, "Rose vs. 1").ruling_text
    # No other case holds the Mosqueda ruling.
    holders = [r for r in out if MOSQUEDA_DISPOSITION in (r.ruling_text or "")]
    assert holders == [mosqueda]


def test_fused_row_text_split_keeps_row_count_and_positions() -> None:
    """Text moves between existing rows, so positional split ids do not shift."""
    before = _load(N16_RULINGS)
    out = _apply_pdf_cache_hit_filters([r.model_copy() for r in before], content_key="x" * 64)
    assert len(out) == len(before)
    assert [r.extracted_case_title for r in out] == [r.extracted_case_title for r in before]


def test_fused_row_text_fresh_extraction_matches_cache_hit() -> None:
    """A fresh join of the page rows and a cache hit on the old entry agree."""
    page_rows = json.loads(N16_PAGE_ROWS.read_text())
    fresh = _join_page_rows(page_rows)
    cache_hit = _apply_pdf_cache_hit_filters(_load(N16_RULINGS), content_key="x" * 64)
    assert [r.model_dump() for r in fresh] == [r.model_dump() for r in cache_hit]


def test_fused_row_text_split_is_a_fixed_point() -> None:
    once = _apply_pdf_post_join_filters(_load(N16_RULINGS))
    twice = _apply_pdf_post_join_filters([r.model_copy() for r in once])
    assert [r.model_dump() for r in once] == [r.model_dump() for r in twice]


def test_fused_row_text_lost_caption_keeps_ruling_off_the_wrong_case() -> None:
    """Cadence (entry 12) is OFF CALENDAR; the ruling after it is entry 13's."""
    before = _load(CADENCE_RULINGS)
    cadence_before = _by_title(before, "Cadence Bank")
    assert cadence_before.ruling_text.startswith("OFF CALENDAR")
    assert "Carrillo" in cadence_before.ruling_text

    out = _apply_pdf_cache_hit_filters([r.model_copy() for r in before], content_key="x" * 64)

    assert len(out) == len(before)
    assert not _by_title(out, "Cadence Bank").ruling_text
    assert not any("Carrillo" in (r.ruling_text or "") for r in out)


def test_fused_row_text_repeated_caption_gives_carrillo_its_own_ruling() -> None:
    """Fresh extraction: entries 12 and 13 share one fused caption."""
    rows = json.loads(CADENCE_PAGE_ROWS.read_text())
    fused = "Cadence Bank N.A. vs. Richardson Carrillo vs. Bryant"
    assert [r["entry_number"] for r in rows if r.get("case_info") == fused] == [12, 13]

    out = _join_page_rows(rows)

    carrillo = _by_title(out, "Carrillo vs. Bryant")
    assert carrillo.entry_number == 13
    assert "Curtis Bryant" in carrillo.ruling_text
    assert not any(
        "Curtis Bryant" in (r.ruling_text or "")
        for r in out
        if (r.extracted_case_title or "").startswith("Cadence")
    )
    # A cache hit on the stored result changes nothing.
    again = _apply_pdf_cache_hit_filters([r.model_copy() for r in out], content_key="x" * 64)
    assert [r.model_dump() for r in again] == [r.model_dump() for r in out]


def test_fused_row_text_second_caption() -> None:
    assert (
        _second_caption("Cadence Bank N.A. vs. Richardson Carrillo vs. Bryant")
        == "Carrillo vs. Bryant"
    )
    assert _second_caption("Cadence Bank N.A. vs. Richardson") is None
    assert _second_caption("A v. B C v. D E v. F") is None


def test_fused_row_text_repeated_caption_guard_rails() -> None:
    fused = "Smith vs. Jones Brown vs. Green"
    rows = [
        _ruling(text="OFF CALENDAR", entry=4, title=fused),
        _ruling(text="Ruling GRANTED.", entry=5, title=fused),
    ]
    out = _assign_repeated_fused_captions(rows)
    assert out[1].extracted_case_title == "Brown vs. Green"
    assert out[0].extracted_case_title == fused
    # Non-consecutive entries, a single caption, or the same case number: unchanged.
    gap = [rows[0], rows[1].model_copy(update={"entry_number": 7})]
    assert _assign_repeated_fused_captions(gap) == gap
    plain = [r.model_copy(update={"extracted_case_title": "Smith vs. Jones"}) for r in rows]
    assert _assign_repeated_fused_captions(plain) == plain
    same_case = [r.model_copy(update={"extracted_case_number": "2026-01500000"}) for r in rows]
    assert _assign_repeated_fused_captions(same_case) == same_case


# ---------------------------------------------------------------------------
# Guard rails
# ---------------------------------------------------------------------------


def test_fused_row_text_leading_stub_segments() -> None:
    assert _split_leading_stub_segments("OFF CALENDAR\n\n**TENTATIVE RULING:**\n\nBody.") == [
        "OFF CALENDAR",
        "**TENTATIVE RULING:**\n\nBody.",
    ]
    assert _split_leading_stub_segments("OFF CALENDAR\nCONTINUED TO 12/16/26") == [
        "OFF CALENDAR",
        "CONTINUED TO 12/16/26",
    ]
    # Body text never opens a boundary, and a stub later in the body is not leading.
    assert len(_split_leading_stub_segments("TENTATIVE RULING:\nBody.\nOFF CALENDAR")) == 1
    assert len(_split_leading_stub_segments("Off Calendar\nMotion to Compel\nThe motion...")) == 1
    assert (
        len(_split_leading_stub_segments("NO TENTATIVE RULING\nCounsel should be prepared.")) == 1
    )


def test_fused_row_text_no_boundary_keeps_text_with_first_case() -> None:
    """#4714's tails (cited cases) are not separate entries: the ruling stays put."""
    rows = [
        _ruling(
            text="TENTATIVE RULING:\n\nThe demurrer is SUSTAINED.",
            entry=4,
            title="White vs. Walmart",
        ),
        _ruling(text=None, title="Berger v. California Insurance Guarantee Assn"),
    ]
    out = _split_fused_row_texts(rows)
    assert out == rows


def test_fused_row_text_segment_count_mismatch_is_left_alone() -> None:
    rows = [
        _ruling(text="OFF CALENDAR\nTENTATIVE RULING:\nBody GRANTED.", entry=4, title="A vs. B"),
        _ruling(text=None, title="C vs. D"),
        _ruling(text=None, title="E vs. F"),
    ]
    assert _split_fused_row_texts(rows) == rows


@pytest.mark.parametrize(
    "tail_title", ["Plaintiff v. Defendant", "A vs. B", "A vs. B 30", "", None]
)
def test_fused_row_text_tail_that_is_not_an_entry_gets_no_text(tail_title: str) -> None:
    rows = [
        _ruling(text="OFF CALENDAR\nTENTATIVE RULING:\nBody GRANTED.", entry=4, title="A vs. B 30"),
        _ruling(text=None, title=tail_title),
    ]
    assert _split_fused_row_texts(rows) == rows


def test_fused_row_text_substantive_second_segment_goes_to_tail() -> None:
    rows = [
        _ruling(
            text="CONTINUED TO 1/5/27\n\nTENTATIVE RULING:\nMotion GRANTED.",
            entry=5,
            title="A vs. B",
        ),
        _ruling(text=None, title="C vs. D", case_number="2026-01500000"),
    ]
    out = _split_fused_row_texts(rows)
    assert out[0].ruling_text is None
    assert out[1].ruling_text == "TENTATIVE RULING:\nMotion GRANTED."
    assert out[1].extracted_case_number == "2026-01500000"


def test_fused_row_text_lost_caption_needs_an_entry_gap() -> None:
    """Without a skipped entry number the stub-then-ruling text is left alone."""
    rows = [
        _ruling(text="OFF CALENDAR\nTENTATIVE RULING:\nBody GRANTED.", entry=12, title="A vs. B"),
        _ruling(text="TENTATIVE RULING:\nOther body.", entry=13, title="C vs. D"),
    ]
    assert _split_fused_row_texts(rows) == rows
    last = [rows[0]]
    assert _split_fused_row_texts(last) == last
    unnumbered = [rows[0].model_copy(update={"entry_number": None}), rows[1]]
    assert _split_fused_row_texts(unnumbered) == unnumbered


def test_fused_row_text_cross_reference_rows_are_exempt() -> None:
    row = _ruling(text="OFF CALENDAR\nTENTATIVE RULING:\nBody.", entry=12).model_copy(
        update={"cross_reference_source": 3}
    )
    rows = [row, _ruling(text=None, title="C vs. D")]
    assert _split_fused_row_texts(rows) == rows


def test_fused_row_text_reattach_does_not_take_a_tail_number_that_now_has_text() -> None:
    """#4717 copies a textless tail's number to the row before it; a tail that got
    its own ruling keeps its number to itself."""
    rows = [
        _ruling(
            text="OFF CALENDAR\n\nTENTATIVE RULING:\nThe demurrer is OVERRULED.",
            entry=2,
            title="A vs. B",
        ),
        _ruling(text=None, title="C vs. D", case_number="2026-01511111"),
    ]
    out = _apply_pdf_post_join_filters(rows)
    assert out[0].extracted_case_number is None
    assert not out[0].ruling_text
    assert out[1].extracted_case_number == "2026-01511111"
    assert "OVERRULED" in out[1].ruling_text


# ---------------------------------------------------------------------------
# Worker: Herrera's slot is skipped, Mosqueda's is dispatched at its old index
# ---------------------------------------------------------------------------


def _make_worker() -> IngestionWorker:
    worker = IngestionWorker(
        redis_client=MagicMock(),
        pg_dsn="postgresql://localhost/test",
        opensearch_client=MagicMock(),
        s3_client=MagicMock(),
        archive_bucket="test-bucket",
    )
    worker._enrichment_client = None
    worker._get_connection = MagicMock(return_value=MagicMock())  # type: ignore[method-assign]
    return worker


@patch("ingestion.worker.delete_stale_split_children", return_value=0)
def test_fused_row_text_worker_dispatches_mosqueda_at_its_own_slot(mock_delete: MagicMock) -> None:
    worker = _make_worker()
    multimodal = MagicMock()
    multimodal.extract_from_pdf.return_value = _apply_pdf_cache_hit_filters(
        _load(N16_RULINGS), content_key="x" * 64
    )
    worker._multimodal_extractor = multimodal
    event = {
        "document_id": "11111111-2222-3333-4444-555555555555",
        "scraper_id": "ca-oc-tentatives-civil",
        "state": "CA",
        "county": "Orange",
        "court": "Superior Court",
        "content_format": "pdf",
        "s3_key": "ca/orange/superior_court/raw/4d92bae3.pdf",
        "hearing_date": "2026-09-09",
    }

    with patch.object(worker, "process_event") as dispatch:
        worker._llm_split_document(
            event, event["document_id"], "", "CA", "Orange", raw_pdf_bytes=b"%PDF-1.7"
        )

    dispatched = [c.args[0] for c in dispatch.call_args_list]
    by_index = {e["_split_index"]: e for e in dispatched}
    # Slot 1 (Herrera) is textless and skipped; slot 2 (Mosqueda) holds the ruling.
    assert 1 not in by_index
    assert MOSQUEDA_DISPOSITION in by_index[2]["ruling_text"]
    assert by_index[2]["document_id"] == make_split_document_id(event["document_id"], 2)
    # Herrera's old child id is not in the valid set, so the stale row is removed.
    valid_ids = mock_delete.call_args.args[2]
    assert make_split_document_id(event["document_id"], 1) not in valid_ids
    assert make_split_document_id(event["document_id"], 2) in valid_ids
