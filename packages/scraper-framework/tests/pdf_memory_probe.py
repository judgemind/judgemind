"""Peak-memory probe for the PDF image paths (#5053).

Run by ``test_pdf_render_memory_5053.py`` in a fresh interpreter, so the
process's peak RSS (``VmHWM`` on Linux, ``ru_maxrss`` on macOS) reflects
only this run, not the rest of the test suite:

    python tests/pdf_memory_probe.py <pdf-path> <mode>

*mode* is ``multimodal`` (``LlmExtractor.extract_from_pdf``), ``ocr``
(``llm_extract.ocr_pdf_document``) or ``render_all`` (the control: the
pre-#5053 behaviour, every page rendered into a list with MuPDF's store
never emptied).  The LLM is mocked.  Prints one JSON object: the RSS peak
before and after the run, and the growth in MB.

Also importable for ``build_scanned_pdf``, the synthetic scan both use.
"""

from __future__ import annotations

import gc
import json
import os
import resource
import sys
from unittest.mock import patch

#: A US letter page in points.
LETTER = (612, 792)


def build_scanned_pdf(pages: int, size_pt: tuple[int, int] = LETTER, dpi: int = 150) -> bytes:
    """A scan-like PDF: every page one full-page RGB noise image.

    The image has the pixel size the page renders at (letter at 150 DPI:
    1275 x 1650, 6.3 MB), and noise does not compress, so each rendered
    page PNG costs its full size: more than a real scanned page, never
    less.  Every page shows the same embedded image, so the PDF itself
    stays one image's size and the measured growth is the rendering's.
    """
    import pymupdf

    width, height = (round(side * dpi / 72) for side in size_pt)
    pix = pymupdf.Pixmap(pymupdf.csRGB, width, height, os.urandom(width * height * 3), False)
    doc = pymupdf.open()
    xref = 0
    for _ in range(pages):
        page = doc.new_page(width=size_pt[0], height=size_pt[1])
        if xref:
            page.insert_image(page.rect, xref=xref)
        else:
            xref = page.insert_image(page.rect, pixmap=pix)
    data = doc.tobytes()
    doc.close()
    return data


def _peak_rss_mb() -> float:
    """This process's peak RSS in MB.

    On Linux, ``VmHWM`` from ``/proc/self/status``: ``ru_maxrss`` survives
    ``execve``, so a probe forked from a 600 MB pytest worker would report
    the worker's peak as its own and every growth as 0.
    """
    try:
        with open("/proc/self/status") as fh:
            for line in fh:
                if line.startswith("VmHWM:"):
                    return int(line.split()[1]) / 1024
    except OSError:
        pass
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Bytes on macOS, KiB on Linux.
    return peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024


class _Llm:
    """A stand-in LLM call that keeps no reference to the page image
    (a ``MagicMock`` records every call's arguments, so it would hold every
    page image itself)."""

    def __init__(self, answer: object) -> None:
        self.answer = answer
        self.calls = 0

    def __call__(self, *_args: object, **_kwargs: object) -> object:
        self.calls += 1
        return self.answer


def _run_multimodal(pdf: bytes) -> int:
    import anthropic

    from framework.llm_extractor import LlmExtractor, _PageExtraction

    with patch.object(anthropic, "Anthropic"):
        ext = LlmExtractor(api_key="probe")
    ext._cache = None
    llm = _Llm(_PageExtraction(status="ok", rows=[], raw_text="[]"))
    with patch.object(ext, "_extract_single_page", llm):
        ext.extract_from_pdf(pdf, strict=True)
    return llm.calls


def _run_ocr(pdf: bytes) -> int:
    from ingestion import llm_extract
    from ingestion.llm_providers import LLMResponse

    llm = _Llm(LLMResponse(text="page text", input_tokens=1, output_tokens=1))
    with patch.object(llm_extract, "call_llm_with_images", llm):
        result = llm_extract.ocr_pdf_document(pdf)
    assert result is not None and result.incomplete is None, result
    return llm.calls


def _run_render_all(pdf: bytes) -> int:
    import pymupdf

    from framework.pdf_render import PdfPageImages

    with patch.object(pymupdf.TOOLS, "store_shrink"):
        pages = list(PdfPageImages(pdf, 10_000))
    return len(pages)


def main() -> None:
    path, mode = sys.argv[1], sys.argv[2]
    with open(path, "rb") as fh:
        pdf = fh.read()
    # Import everything first, so the baseline includes the code.
    import framework.llm_extractor  # noqa: F401
    import ingestion.llm_extract  # noqa: F401

    gc.collect()
    before = _peak_rss_mb()
    runner = {"multimodal": _run_multimodal, "ocr": _run_ocr, "render_all": _run_render_all}
    pages = runner[mode](pdf)
    after = _peak_rss_mb()
    print(
        json.dumps(
            {
                "mode": mode,
                "pages": pages,
                "peak_before_mb": round(before, 1),
                "peak_after_mb": round(after, 1),
                "growth_mb": round(after - before, 1),
            }
        )
    )


if __name__ == "__main__":
    main()
