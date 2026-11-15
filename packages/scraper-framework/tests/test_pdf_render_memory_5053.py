"""Memory guards for the PDF image paths (#5053).

The live ingestion worker runs in 512 MB.  #5010 / #5042: OCR rendered
every page of a 25-page scan before reading any, MuPDF kept every decoded
page image in its store, and the worker was OOM-killed on every
redelivery.  #5050 streamed OCR; the multimodal split
(``LlmExtractor.extract_from_pdf``, up to 500 pages since #4995) still
rendered every page up front.  Both now go through one streaming renderer,
``framework.pdf_render``: a page is rendered when it is about to be sent,
and MuPDF's store is emptied after each page.

These tests fail on a regression to render-everything:

- in process, deterministically: the number of rendered pages alive when
  each page's LLM call is made (multimodal: 1; OCR: ``concurrency``);
- in a fresh interpreter (``pdf_memory_probe.py``), the peak RSS growth of
  a synthetic 64-page letter-size scan through each path, LLM mocked.

The RSS bound
-------------
The worker's idle RSS is about 200 MB: #5050 measured the 25-page San
Bernardino scan at ~100 MB over it once streamed and ~300 MB before, which
hit 512 MB.  That leaves ~300 MB for a document.  The synthetic page is
worse than any real scan (an incompressible 1275 x 1650 RGB noise image:
a 6.3 MB PNG; a real 150 DPI scan page is about 1 MB), and one such page
in flight costs ~100 MB (decoded image, pixmap, PNG encoder buffers, the
PNG; OCR has ``concurrency`` = 4 pages in flight).  So:

- ``PEAK_GROWTH_BUDGET_MB`` = 160: the growth over the probe's baseline for
  the 64-page scan, on either path.  Rendering all 64 pages up front
  costs ~450 MB.
- ``PAGE_SCALING_BUDGET_MB`` = 32: how much more the 64-page scan may cost
  than an 8-page one.  Streamed, memory does not depend on the page count
  (measured: within a few MB); rendered up front, each extra page adds
  its PNG (~6.3 MB, ~350 MB for the 56 extra pages).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import anthropic
import pymupdf
import pytest
from pdf_memory_probe import build_scanned_pdf

from framework import llm_extractor, pdf_render
from framework.llm_extractor import LlmExtractor, _PageExtraction
from framework.pdf_render import PNG_MEDIA_TYPE, PdfPageImages
from ingestion import llm_extract
from ingestion.llm_providers import LLMResponse

PEAK_GROWTH_BUDGET_MB = 160
PAGE_SCALING_BUDGET_MB = 32
LONG_SCAN_PAGES = 64
SHORT_SCAN_PAGES = 8

TESTS_DIR = Path(__file__).resolve().parent
PROBE = TESTS_DIR / "pdf_memory_probe.py"
SRC_DIR = TESTS_DIR.parent / "src"


def _small_scan(pages: int) -> bytes:
    """A fast scan-like PDF for the in-process tests (small pages)."""
    return build_scanned_pdf(pages, size_pt=(100, 100))


def _extractor() -> LlmExtractor:
    with patch.object(anthropic, "Anthropic"):
        ext = LlmExtractor(api_key="test-key")
    ext._cache = None
    return ext


# ---------------------------------------------------------------------------
# In process: pages alive per LLM call
# ---------------------------------------------------------------------------


class _AliveCounter:
    """Counts pages rendered by ``pdf_render.render_page`` against pages
    answered by the (mocked) LLM, recording the most alive at once."""

    def __init__(self) -> None:
        self.rendered = 0
        self.answered = 0
        self.max_alive = 0
        self._real = pdf_render.render_page

    def render(self, doc: Any, index: int, **kwargs: Any) -> bytes:
        self.rendered += 1
        self.max_alive = max(self.max_alive, self.rendered - self.answered)
        return self._real(doc, index, **kwargs)


def test_multimodal_memory_renders_each_page_just_before_its_llm_call() -> None:
    """``extract_from_pdf`` renders page N only when its call is next: one
    rendered page alive at any call.  Rendering every page up front (the
    pre-#5053 ``_render_pdf_pages``) has all 64 alive at the first call."""
    counter = _AliveCounter()
    alive_at_call: list[int] = []

    def llm(*_args: Any, **_kwargs: Any) -> _PageExtraction:
        alive_at_call.append(counter.rendered - counter.answered)
        counter.answered += 1
        return _PageExtraction(status="ok", rows=[], raw_text="[]")

    ext = _extractor()
    with (
        patch.object(pdf_render, "render_page", side_effect=counter.render),
        patch.object(ext, "_extract_single_page", side_effect=llm),
    ):
        assert ext.extract_from_pdf(_small_scan(LONG_SCAN_PAGES), strict=True) == []

    assert counter.rendered == LONG_SCAN_PAGES
    assert alive_at_call == [1] * LONG_SCAN_PAGES
    assert counter.max_alive == 1


def test_multimodal_memory_empties_mupdf_store_per_page() -> None:
    ext = _extractor()
    with (
        patch.object(pymupdf.TOOLS, "store_shrink") as shrink,
        patch.object(
            ext,
            "_extract_single_page",
            return_value=_PageExtraction(status="ok", rows=[], raw_text="[]"),
        ),
    ):
        ext.extract_from_pdf(_small_scan(5), strict=True)
    # One per rendered page, plus one per page of the text-layer pass.
    assert shrink.call_count == 10
    shrink.assert_called_with(100)


def test_ocr_memory_renders_at_most_concurrency_pages_ahead() -> None:
    """OCR goes through the same renderer and keeps at most *concurrency*
    pages alive (#5050)."""
    counter = _AliveCounter()

    def call(**_kwargs: Any) -> LLMResponse:
        counter.answered += 1
        return LLMResponse(text="page text", input_tokens=1, output_tokens=1)

    with (
        patch.object(pdf_render, "render_page", side_effect=counter.render),
        patch.object(llm_extract, "render_page", side_effect=counter.render),
        patch.object(llm_extract, "call_llm_with_images", side_effect=call),
    ):
        result = llm_extract.ocr_pdf_document(_small_scan(LONG_SCAN_PAGES), concurrency=2)

    assert result is not None and result.incomplete is None
    assert counter.rendered == LONG_SCAN_PAGES
    assert counter.max_alive <= 2


def test_text_layer_pass_skips_image_blocks() -> None:
    """The recovery text-layer pass reads text blocks only: asking for
    image blocks made MuPDF decode every page's scan into its store, and
    put an ``<image: ...>`` placeholder in the page text."""
    doc = pymupdf.open(stream=_small_scan(1), filetype="pdf")
    doc[0].insert_text((10, 20), "Case 42", fontsize=8)
    pdf = doc.tobytes()
    doc.close()
    layers = llm_extractor._pdf_page_text_layers(pdf, 5)
    assert layers == ["Case 42"]


# ---------------------------------------------------------------------------
# PdfPageImages
# ---------------------------------------------------------------------------


class TestPdfPageImages:
    def test_renders_nothing_until_a_page_is_reached(self) -> None:
        with patch.object(pdf_render, "render_page") as render:
            pages = PdfPageImages(_small_scan(4), max_pages=10)
            assert len(pages) == 4
            assert pages.total_pages == 4
        render.assert_not_called()

    def test_caps_at_max_pages(self) -> None:
        pages = PdfPageImages(_small_scan(4), max_pages=2)
        assert len(pages) == 2
        assert pages.total_pages == 4
        assert len(list(pages)) == 2

    def test_negative_max_pages_is_empty(self) -> None:
        assert len(PdfPageImages(_small_scan(2), max_pages=-1)) == 0

    def test_index_renders_one_page(self) -> None:
        pages = PdfPageImages(_small_scan(3), max_pages=10)
        png, media_type = pages[-1]
        assert media_type == PNG_MEDIA_TYPE
        assert png[:4] == b"\x89PNG"
        assert pages[2] == (png, PNG_MEDIA_TYPE)
        assert [page for page in pages][2] == (png, PNG_MEDIA_TYPE)

    def test_index_out_of_range(self) -> None:
        pages = PdfPageImages(_small_scan(2), max_pages=10)
        with pytest.raises(IndexError):
            pages[2]
        with pytest.raises(IndexError):
            pages[-3]

    def test_slice_is_refused(self) -> None:
        pages = PdfPageImages(_small_scan(2), max_pages=10)
        with pytest.raises(TypeError):
            pages[0:2]  # type: ignore[index]

    def test_unreadable_pdf_raises_at_construction(self) -> None:
        with pytest.raises(Exception):
            PdfPageImages(b"not a pdf", max_pages=10)

    def test_multimodal_renderer_is_lazy(self) -> None:
        assert isinstance(llm_extractor._render_pdf_pages(_small_scan(2), 10), PdfPageImages)


# ---------------------------------------------------------------------------
# Fresh interpreter: peak RSS
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def scans(tmp_path_factory: pytest.TempPathFactory) -> dict[int, Path]:
    root = tmp_path_factory.mktemp("scans")
    paths = {}
    for pages in (SHORT_SCAN_PAGES, LONG_SCAN_PAGES):
        path = root / f"scan-{pages}.pdf"
        path.write_bytes(build_scanned_pdf(pages))
        paths[pages] = path
    return paths


def _probe(pdf: Path, mode: str) -> dict[str, Any]:
    env = {**os.environ, "PYTHONPATH": str(SRC_DIR), "COVERAGE_PROCESS_START": ""}
    proc = subprocess.run(
        [sys.executable, str(PROBE), str(pdf), mode],
        capture_output=True,
        text=True,
        env=env,
        timeout=240,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-4000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("mode", ["multimodal", "ocr"])
def test_memory_peak_rss_is_bounded_and_independent_of_page_count(
    mode: str, scans: dict[int, Path]
) -> None:
    short = _probe(scans[SHORT_SCAN_PAGES], mode)
    long = _probe(scans[LONG_SCAN_PAGES], mode)
    assert long["pages"] == LONG_SCAN_PAGES
    assert long["growth_mb"] <= PEAK_GROWTH_BUDGET_MB, long
    assert long["growth_mb"] - short["growth_mb"] <= PAGE_SCALING_BUDGET_MB, (short, long)


def test_memory_probe_detects_render_all(scans: dict[int, Path]) -> None:
    """The control: rendering every page up front, as before #5053, is
    over the budget, so the RSS guard above is not vacuous here."""
    result = _probe(scans[LONG_SCAN_PAGES], "render_all")
    assert result["growth_mb"] > PEAK_GROWTH_BUDGET_MB + PAGE_SCALING_BUDGET_MB, result
